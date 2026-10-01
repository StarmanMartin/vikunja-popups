from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "vikunja-popups"
CONFIG_FILE = CONFIG_DIR / "config.json"
# Runtime state (last read email, pending AI proposals), not configuration.
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "vikunja-popups"
STATE_FILE = STATE_DIR / "state.json"


EMAIL_SECURITY = ("ssl", "starttls", "none")


@dataclass(frozen=True)
class EmailConfig:
    address: str = ""
    username: str = ""
    password: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_security: str = "ssl"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: str = "starttls"


@dataclass(frozen=True)
class GitHubConfig:
    username: str = ""
    token: str = ""
    api_url: str = "https://api.github.com"


@dataclass(frozen=True)
class Config:
    base_url: str
    token: str
    refresh_seconds: int = 60
    include_done: bool = False
    popup_width: int = 380
    margin_top: int = 18
    margin_right: int = 18
    gap: int = 10
    max_visible: int = 12
    verify_tls: bool = True
    ai_model: str = ""
    email: EmailConfig = EmailConfig()
    github: GitHubConfig = GitHubConfig()


EXAMPLE = {
    "base_url": "https://vikunja.example.com",
    "token": "",
    "refresh_seconds": 60,
    "include_done": False,
    "popup_width": 380,
    "margin_top": 18,
    "margin_right": 18,
    "gap": 10,
    "max_visible": 12,
    "verify_tls": True,
    "ai_model": "",
    "email": {
        "address": "",
        "username": "",
        "password": "",
        "imap_host": "",
        "imap_port": 993,
        "imap_security": "ssl",
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_security": "starttls",
    },
    "github": {
        "username": "",
        "token": "",
        "api_url": "https://api.github.com",
    },
}


def ensure_example_config() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(EXAMPLE, indent=2) + "\n", encoding="utf-8")
        CONFIG_FILE.chmod(0o600)


def read_raw_config() -> dict:
    """The config file as a dict, without validation (for the settings app)."""
    ensure_example_config()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {**EXAMPLE, **data}


def write_private_json(path: Path, data: object) -> None:
    """Atomically write JSON readable only by the user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # Create it with mode 0600 from the start: it holds secrets.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2) + "\n")
    tmp.chmod(0o600)
    os.replace(tmp, path)


def save_raw_config(data: dict) -> None:
    write_private_json(CONFIG_FILE, data)


def normalize_email(raw: object) -> dict:
    """The `email` section with defaults filled in and bad values replaced.

    Lenient on purpose: a broken email section must not stop the popups.
    """
    defaults = EXAMPLE["email"]
    raw = raw if isinstance(raw, dict) else {}
    email = {**raw}
    for key, default in defaults.items():
        value = raw.get(key, default)
        if isinstance(default, int):
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = default
            if not 1 <= value <= 65535:
                value = default
        elif key == "password":
            value = str(value or "")
        else:
            value = str(value or "").strip()
        email[key] = value
    for key in ("imap_security", "smtp_security"):
        if email[key] not in EMAIL_SECURITY:
            email[key] = defaults[key]
    return email


def normalize_github(raw: object) -> dict:
    """The `github` section with defaults filled in, lenient like the email one."""
    defaults = EXAMPLE["github"]
    raw = raw if isinstance(raw, dict) else {}
    github = {**raw}
    for key, default in defaults.items():
        github[key] = str(raw.get(key) or "").strip()
    github["api_url"] = github["api_url"].rstrip("/") or defaults["api_url"]
    return github


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    # Mode 0600: pending proposals contain email text.
    write_private_json(STATE_FILE, state)


def email_config_from(raw: object) -> EmailConfig:
    email = normalize_email(raw)
    return EmailConfig(**{key: email[key] for key in EXAMPLE["email"]})


def github_config_from(raw: object) -> GitHubConfig:
    github = normalize_github(raw)
    return GitHubConfig(**{key: github[key] for key in EXAMPLE["github"]})


def load_config() -> Config:
    ensure_example_config()
    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    token = os.environ.get("VIKUNJA_TOKEN", data.get("token", "")).strip()
    base_url = str(data.get("base_url", "")).strip()

    if not base_url or base_url == "https://vikunja.example.com":
        raise ValueError(f"Set base_url in {CONFIG_FILE}")
    if not token:
        raise ValueError(
            f"Set VIKUNJA_TOKEN or token in {CONFIG_FILE}"
        )

    return Config(
        base_url=base_url,
        token=token,
        refresh_seconds=max(10, int(data.get("refresh_seconds", 60))),
        include_done=bool(data.get("include_done", False)),
        popup_width=max(260, int(data.get("popup_width", 380))),
        margin_top=max(0, int(data.get("margin_top", 18))),
        margin_right=max(0, int(data.get("margin_right", 18))),
        gap=max(0, int(data.get("gap", 10))),
        max_visible=max(1, int(data.get("max_visible", 12))),
        verify_tls=bool(data.get("verify_tls", True)),
        ai_model=str(data.get("ai_model") or "").strip(),
        email=email_config_from(data.get("email")),
        github=github_config_from(data.get("github")),
    )
