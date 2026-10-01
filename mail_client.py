from __future__ import annotations

import email
import email.policy
import html
import imaplib
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from config import EmailConfig


TIMEOUT = 15
# Longest email body (in characters) that is kept and sent to the AI.
MAX_TEXT_CHARS = 12000
# IMAP dates use English month names; strftime("%b") follows the locale,
# which GTK sets from the environment.
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class MailError(RuntimeError):
    """Raised when the mail server cannot be used."""


@dataclass(frozen=True)
class MailMessage:
    key: str          # "<uidvalidity>:<uid>", unique within the mailbox
    timestamp: float  # IMAP INTERNALDATE (arrival on the server), epoch seconds
    sender: str
    reply_to: str     # address an answer goes to
    to: str
    subject: str
    date: str
    message_id: str
    references: str
    text: str


def _user(account: EmailConfig) -> str:
    return account.username or account.address


def connect_imap(account: EmailConfig) -> imaplib.IMAP4:
    """A logged-in IMAP connection. Use it as a context manager."""
    context = ssl.create_default_context()
    if account.imap_security == "ssl":
        imap = imaplib.IMAP4_SSL(
            account.imap_host, account.imap_port, ssl_context=context, timeout=TIMEOUT
        )
    else:
        imap = imaplib.IMAP4(account.imap_host, account.imap_port, timeout=TIMEOUT)
    try:
        if account.imap_security == "starttls":
            imap.starttls(ssl_context=context)
        imap.login(_user(account), account.password)
    except Exception:
        imap.shutdown()
        raise
    return imap


def connect_smtp(account: EmailConfig) -> smtplib.SMTP:
    """A logged-in SMTP connection. Use it as a context manager."""
    context = ssl.create_default_context()
    if account.smtp_security == "ssl":
        smtp = smtplib.SMTP_SSL(
            account.smtp_host, account.smtp_port, context=context, timeout=TIMEOUT
        )
    else:
        smtp = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=TIMEOUT)
    try:
        if account.smtp_security == "starttls":
            smtp.starttls(context=context)
        smtp.login(_user(account), account.password)
    except Exception:
        smtp.close()
        raise
    return smtp


def test_login(account: EmailConfig) -> str:
    """Log in to the configured IMAP and SMTP servers. Blocking."""
    checked = []
    if account.imap_host:
        try:
            with connect_imap(account):
                pass
        except Exception as exc:
            raise MailError(f"IMAP login failed: {exc}") from exc
        checked.append("IMAP")
    if account.smtp_host:
        try:
            with connect_smtp(account):
                pass
        except Exception as exc:
            raise MailError(f"SMTP login failed: {exc}") from exc
        checked.append("SMTP")
    if not checked:
        raise MailError("Enter an IMAP or SMTP server.")
    return f"Email login OK ({' and '.join(checked)})."


def html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style).*?</\1>", "", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", "", markup))
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def _body_text(message: email.message.EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, ValueError):
        payload = part.get_payload(decode=True) or b""
        content = payload.decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        content = html_to_text(content)
    text = content.strip()
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n[... shortened]"
    return text


def _imap_date(timestamp: float) -> str:
    day = time.gmtime(timestamp)
    return f"{day.tm_mday:02d}-{MONTHS[day.tm_mon - 1]}-{day.tm_year}"


def _check(result: tuple, what: str) -> list:
    status, data = result
    if status != "OK":
        raise MailError(f"IMAP {what} failed: {data}")
    return data


def fetch_new_messages(
    account: EmailConfig,
    since: float,
    seen_keys: set[str],
    limit: int,
) -> list[MailMessage]:
    """INBOX messages that arrived at or after `since`, oldest first.

    Messages whose key is in `seen_keys` are skipped (they arrived in the same
    second as the last one that was handled). At most `limit` are returned;
    the rest follow on the next call. Messages are not marked as read.
    """
    with connect_imap(account) as imap:
        _check(imap.select("INBOX", readonly=True), "SELECT")
        uidvalidity = (imap.response("UIDVALIDITY")[1] or [b"0"])[0]
        uidvalidity = (uidvalidity or b"0").decode()

        # SEARCH SINCE only compares dates, so ask one day earlier (time
        # zones) and filter by the exact arrival time below.
        data = _check(imap.uid("SEARCH", None, "SINCE", _imap_date(since - 86400)), "SEARCH")
        uids = data[0].split() if data and data[0] else []
        if not uids:
            return []

        candidates = []
        data = _check(imap.uid("FETCH", b",".join(uids), "(INTERNALDATE)"), "FETCH")
        for item in data:
            raw = item[0] if isinstance(item, tuple) else item
            if not isinstance(raw, bytes):
                continue
            uid = re.search(rb"UID (\d+)", raw)
            arrived = imaplib.Internaldate2tuple(raw)
            if uid is None or arrived is None:
                continue
            timestamp = time.mktime(arrived)
            key = f"{uidvalidity}:{uid.group(1).decode()}"
            if timestamp >= since and key not in seen_keys:
                candidates.append((timestamp, int(uid.group(1)), key))
        candidates.sort()

        messages = []
        for timestamp, uid, key in candidates[:limit]:
            data = _check(imap.uid("FETCH", str(uid), "(BODY.PEEK[])"), "FETCH")
            raw = next((item[1] for item in data if isinstance(item, tuple)), None)
            if raw is None:
                continue
            parsed = email.message_from_bytes(raw, policy=email.policy.default)
            sender = str(parsed.get("From", ""))
            reply_to = parseaddr(str(parsed.get("Reply-To") or sender))[1]
            messages.append(
                MailMessage(
                    key=key,
                    timestamp=timestamp,
                    sender=sender,
                    reply_to=reply_to,
                    to=str(parsed.get("To", "")),
                    subject=str(parsed.get("Subject", "")),
                    date=str(parsed.get("Date", "")),
                    message_id=str(parsed.get("Message-ID", "")).strip(),
                    references=str(parsed.get("References", "")).strip(),
                    text=_body_text(parsed),
                )
            )
        return messages


def send_reply(account: EmailConfig, original: dict, body: str) -> None:
    """Answer `original` (a MailMessage as dict) with `body`, quoting it."""
    if not original.get("reply_to"):
        raise MailError("The email has no address to reply to.")

    reply = EmailMessage()
    reply["From"] = account.address
    reply["To"] = original["reply_to"]
    subject = original.get("subject", "")
    reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    reply["Date"] = formatdate(localtime=True)
    domain = account.address.rpartition("@")[2] or None
    reply["Message-ID"] = make_msgid(domain=domain)
    message_id = original.get("message_id", "")
    if message_id:
        reply["In-Reply-To"] = message_id
        reply["References"] = f"{original.get('references', '')} {message_id}".strip()

    quoted = "\n".join(f"> {line}" for line in original.get("text", "").splitlines())
    reply.set_content(
        f"{body.rstrip()}\n\n{original.get('date', '')}, {original.get('sender', '')}:\n{quoted}\n"
    )

    with connect_smtp(account) as smtp:
        smtp.send_message(reply)
