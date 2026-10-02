from __future__ import annotations

import html
import json
from dataclasses import dataclass
from typing import Any, Iterable

import requests


class VikunjaError(RuntimeError):
    """Raised when the Vikunja API cannot be queried successfully."""


@dataclass(frozen=True)
class VikunjaProject:
    id: int
    title: str

    @classmethod
    def from_api(cls, value: dict[str, Any]) -> "VikunjaProject":
        return cls(
            id=int(value["id"]),
            title=str(value.get("title") or f"Project {value['id']}"),
        )


@dataclass(frozen=True)
class VikunjaTask:
    id: int
    title: str
    description: str = ""
    done: bool = False
    priority: int = 0
    due_date: str | None = None
    updated: str | None = None
    project_id: int | None = None

    @classmethod
    def from_api(cls, value: dict[str, Any]) -> "VikunjaTask":
        return cls(
            id=int(value["id"]),
            title=str(value.get("title") or f"Task {value['id']}"),
            description=str(value.get("description") or ""),
            done=bool(value.get("done", False)),
            priority=int(value.get("priority") or 0),
            due_date=value.get("due_date"),
            updated=value.get("updated"),
            project_id=value.get("project_id"),
        )


class VikunjaClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 15.0,
        verify_tls: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if not self.base_url.endswith("/api/v2"):
            self.base_url += "/api/v2"

        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify_tls
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "vikunja-popups/1.0",
            }
        )

    def _get(self, path: str, **params: Any) -> requests.Response:
        return self._request("GET", path, params=params)

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                timeout=self.timeout,
                **kwargs,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            message = str(exc)
            response = getattr(exc, "response", None)
            if response is not None:
                try:
                    body = response.json()
                    message = body.get("detail") or body.get("message") or message
                except Exception:
                    pass
            raise VikunjaError(message) from exc

    def _paginated(self, path: str, per_page: int) -> Iterable[dict[str, Any]]:
        """Yield every item of a collection endpoint, following v2 pagination."""
        page = 1

        while True:
            payload = self._get(path, page=page, per_page=per_page).json()

            # v2 returns a paginated object. Keeping the list fallback makes the
            # client tolerant of older/self-hosted installations.
            if isinstance(payload, dict):
                raw_items = payload.get("items", [])
                total_pages = int(payload.get("total_pages") or 1)
            elif isinstance(payload, list):
                raw_items = payload
                total_pages = 1
            else:
                raise VikunjaError("Unexpected response format from Vikunja")

            for item in raw_items:
                if isinstance(item, dict):
                    yield item

            if page >= total_pages:
                break
            page += 1

    def get_projects(self, *, per_page: int = 100) -> list[VikunjaProject]:
        """Every project the token can see.

        Archived projects are skipped, as are the server's pseudo projects
        ("Favorites", "My Open Tasks"), which come back with negative ids and
        do not accept new tasks.
        """
        return [
            VikunjaProject.from_api(item)
            for item in self._paginated("/projects", per_page)
            if int(item.get("id") or 0) > 0 and not item.get("is_archived", False)
        ]

    def create_project(self, title: str) -> VikunjaProject:
        response = self._request("POST", "/projects", json={"title": title})
        return VikunjaProject.from_api(response.json())

    def ensure_project(self, title: str) -> VikunjaProject:
        """The project called `title` (case-insensitive), created when missing."""
        for project in self.get_projects():
            if project.title.strip().casefold() == title.casefold():
                return project
        return self.create_project(title)

    def get_project_tasks(
        self,
        project_id: int | str,
        *,
        include_done: bool = False,
        per_page: int = 1000,
    ) -> list[VikunjaTask]:
        """Fetch every task from a project, following v2 pagination."""
        tasks = [
            VikunjaTask.from_api(item)
            for item in self._paginated(f"/projects/{project_id}/tasks", per_page)
        ]

        if not include_done:
            tasks = [task for task in tasks if not task.done]

        return tasks

    def create_task(
        self,
        project_id: int | str,
        title: str,
        *,
        description: str = "",
        priority: int = 0,
        due_date: str | None = None,
    ) -> VikunjaTask:
        """Create a task in a project. `description` is HTML."""
        body: dict[str, Any] = {"title": title}
        if description:
            body["description"] = description
        if priority:
            body["priority"] = priority
        if due_date:
            body["due_date"] = due_date
        response = self._request("POST", f"/projects/{project_id}/tasks", json=body)
        return VikunjaTask.from_api(response.json())

    def get_task(self, task_id: int | str) -> VikunjaTask:
        return VikunjaTask.from_api(self._get(f"/tasks/{task_id}").json())

    def update_task(self, task_id: int | str, changes: dict[str, Any]) -> VikunjaTask:
        """Change only the given fields (JSON merge patch)."""
        response = self._request(
            "PATCH",
            f"/tasks/{task_id}",
            data=json.dumps(changes),
            headers={"Content-Type": "application/merge-patch+json"},
        )
        return VikunjaTask.from_api(response.json())

    def add_comment(self, task_id: int | str, comment: str) -> None:
        """Add a comment (HTML) to a task."""
        self._request("POST", f"/tasks/{task_id}/comments", json={"comment": comment})


def text_to_html(text: str) -> str:
    """Plain text as the HTML Vikunja stores in descriptions and comments."""
    paragraphs = [part.strip() for part in text.strip().split("\n\n") if part.strip()]
    return "".join(
        f"<p>{html.escape(part).replace(chr(10), '<br>')}</p>" for part in paragraphs
    )
