from __future__ import annotations

from urllib.parse import urlsplit

import requests

from config import GitHubConfig


TIMEOUT = 15


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
                "Authorization": f"Bearer {account.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
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
