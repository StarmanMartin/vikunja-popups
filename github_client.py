from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from config import GitHubConfig


TIMEOUT = 15
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
# Longest issue body or comment thread (in characters) sent to the AI.
MAX_TEXT_CHARS = 12000
# Pages of 100 followed per list, so a huge result cannot stall a check.
MAX_PAGES = 5


class GitHubError(RuntimeError):
    """Raised when the GitHub API cannot be used."""


def token_page_url(api_url: str) -> str:
    """The web page that creates a fine-grained personal access token."""
    parts = urlsplit(api_url)
    host = parts.netloc or "api.github.com"
    # github.com serves its API from api.github.com, Enterprise from <host>/api/v3.
    if host == "api.github.com":
        host = "github.com"
    return f"{parts.scheme or 'https'}://{host}/settings/personal-access-tokens/new"


def _get_user(account: GitHubConfig) -> requests.Response:
    """GET /user with the account's token. Blocking."""
    if not account.token:
        raise GitHubError("Enter a GitHub token.")
    try:
        response = requests.get(
            f"{account.api_url}/user",
            headers={
**HEADERS, "Authorization": f"Bearer {account.token}"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GitHubError(f"GitHub login failed: {exc}") from exc

    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if not response.ok:
        message = body.get("message") or response.reason
        raise GitHubError(f"GitHub login failed: {response.status_code} {message}")
    if not body.get("login"):
        raise GitHubError("GitHub login failed: the answer has no login name.")
    return response


def get_login(account: GitHubConfig) -> str:
    """The login name the token belongs to. Blocking."""
    return str(_get_user(account).json()["login"])


def test_login(account: GitHubConfig) -> str:
    """Check the token against GET /user. Blocking."""
    response = _get_user(account)
    text = f"GitHub login OK: {response.json()['login']}."
    # Only classic tokens report their scopes; fine-grained tokens don't.
    scopes = response.headers.get("X-OAuth-Scopes")
    if scopes is not None:
        text += f" Scopes: {scopes or 'none'}."
    return text


@dataclass(frozen=True)
class GitHubItem:
    """One thing on GitHub the AI is asked about."""

    key: str          # unique per question, e.g. "gh:owner/repo#12:assigned"
    ref: str          # "owner/repo#12"
    kind: str         # "assigned" or "comments" (new comments on the user's PR)
    is_pr: bool
    title: str
    url: str
    author: str       # who opened the issue/PR, or who wrote the new comments
    timestamp: float  # epoch seconds: last update, or the newest comment
    text: str         # the body, or the new comments


def _parse_time(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shorten(text: str) -> str:
    text = text.strip()
    return text if len(text) <= MAX_TEXT_CHARS else text[: MAX_TEXT_CHARS - 1] + "…"


class GitHubClient:
    def __init__(self, account: GitHubConfig) -> None:
        self.api_url = account.api_url
        self.session = requests.Session()
        self.session.headers.update({**HEADERS, "Authorization": f"Bearer {account.token}"})

    def _get_all(self, path: str, params: dict | None = None) -> list:
        """Every item of a list endpoint (or of a search), following Link headers."""
        url: str | None = f"{self.api_url}{path}"
        params = {**(params or {}), "per_page": 100}
        items: list = []
        for _page in range(MAX_PAGES):
            if url is None:
                break
            try:
                response = self.session.get(url, params=params, timeout=TIMEOUT)
            except requests.RequestException as exc:
                raise GitHubError(f"GitHub request failed: {exc}") from exc
            if not response.ok:
                try:
                    message = response.json().get("message")
                except (ValueError, AttributeError):
                    message = None
                raise GitHubError(
                    f"GitHub {path} failed: {response.status_code} {message or response.reason}"
                )
            body = response.json()
            items.extend(body.get("items", []) if isinstance(body, dict) else body)
            # The next link already carries the query.
            url, params = response.links.get("next", {}).get("url"), None
        return items

    def _search(self, query: str) -> list[dict]:
        return self._get_all("/search/issues", {"q": query, "sort": "updated", "order": "asc"})

    @staticmethod
    def _ref(item: dict) -> tuple[str, str, int]:
        # repository_url is ".../repos/<owner>/<repo>".
        repo = "/".join(str(item.get("repository_url", "")).split("/")[-2:])
        return f"{repo}#{item['number']}", repo, int(item["number"])

    def assigned(self, login: str) -> list[GitHubItem]:
        """Open issues and pull requests assigned to `login`, oldest update first."""
        items = []
        for raw in self._search(f"assignee:{login} is:open archived:false"):
            ref, _repo, _number = self._ref(raw)
            items.append(
                GitHubItem(
                    key=f"gh:{ref}:assigned",
                    ref=ref,
                    kind="assigned",
                    is_pr="pull_request" in raw,
                    title=str(raw.get("title") or ""),
                    url=str(raw.get("html_url") or ""),
                    author=str((raw.get("user") or {}).get("login") or ""),
                    timestamp=_parse_time(raw.get("updated_at")),
                    text=_shorten(str(raw.get("body") or "")),
                )
            )
        return items

    def new_pr_comments(self, login: str, since: float, until: float) -> list[GitHubItem]:
        """Comments and reviews by others on `login`'s pull requests, written in [since, until).

        One item per pull request, holding all of its new comments.
        """
        items = []
        for raw in self._search(f"is:pr author:{login} archived:false updated:>={_iso(since)}"):
            ref, repo, number = self._ref(raw)
            entries = []  # (API object, what it is)
            for comment in self._get_all(f"/repos/{repo}/issues/{number}/comments", {"since": _iso(since)}):
                entries.append((comment, "comment"))
            for comment in self._get_all(f"/repos/{repo}/pulls/{number}/comments", {"since": _iso(since)}):
                entries.append((comment, f"review comment on {comment.get('path', '?')}"))
            for review in self._get_all(f"/repos/{repo}/pulls/{number}/reviews"):
                # A plain "COMMENTED" review without text only wraps review comments.
                if review.get("body") or review.get("state") in ("APPROVED", "CHANGES_REQUESTED"):
                    entries.append((review, f"review: {review.get('state', '?').lower()}"))

            new = []
            for entry, what in entries:
                author = str((entry.get("user") or {}).get("login") or "")
                created = _parse_time(entry.get("created_at") or entry.get("submitted_at"))
                if author.lower() == login.lower() or not since <= created < until:
                    continue
                new.append((created, int(entry.get("id") or 0), author, what, str(entry.get("body") or "")))
            if not new:
                continue
            new.sort()
            text = "\n\n".join(
                f"[{datetime.fromtimestamp(created).astimezone().isoformat(timespec='minutes')}]"
                f" {author} ({what}):\n{body.strip() or '(no text)'}"
                for created, _id, author, what, body in new
            )
            authors = list(dict.fromkeys(author for _c, _i, author, _w, _b in new))
            items.append(
                GitHubItem(
                    # The newest comment id: newer comments make a new question.
                    key=f"gh:{ref}:comments:{max(entry_id for _c, entry_id, *_rest in new)}",
                    ref=ref,
                    kind="comments",
                    is_pr=True,
                    title=str(raw.get("title") or ""),
                    url=str(raw.get("html_url") or ""),
                    author=", ".join(authors),
                    timestamp=new[-1][0],
                    text=_shorten(text),
                )
            )
        items.sort(key=lambda item: item.timestamp)
        return items
