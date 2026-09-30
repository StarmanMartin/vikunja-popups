from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "vikunja-popups"
CONFIG_FILE = CONFIG_DIR / "config.json"


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


def save_raw_config(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
    # Create it with mode 0600 from the start: it holds the API token.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2) + "\n")
    tmp.chmod(0o600)
    os.replace(tmp, CONFIG_FILE)


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
    )
