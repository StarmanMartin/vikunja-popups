from __future__ import annotations

import html
import logging
import os
import re
import signal
import threading
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")

try:
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import GtkLayerShell  # type: ignore
except (ValueError, ImportError):
    GtkLayerShell = None

# GNOME (Mutter) does not implement wlr-layer-shell and ignores client window
# positioning on Wayland. Run through XWayland there so move() works. Must be
# set before Gtk is imported, because importing Gtk opens the display.
if GtkLayerShell is None or "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", ""):
    GtkLayerShell = None
    os.environ["GDK_BACKEND"] = "x11"

from gi.repository import Gio, GLib, Gtk, Gdk  # type: ignore  # noqa: E402

from config import CONFIG_FILE, Config, load_config
from vikunja_client import VikunjaClient, VikunjaProject, VikunjaTask


LOG = logging.getLogger("vikunja-popups")


CSS = b"""
/* The window itself is transparent; only the rounded card box is painted. */
.popup-window {
    background-color: transparent;
}
.task-popup {
    background: rgba(35, 35, 35, 0.96);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 10px;
    padding: 2px;
}
.task-title {
    color: #ffffff;
    font-weight: bold;
    font-size: 14px;
}
.task-title link {
    color: #ffffff;
}
.task-title link:hover {
    color: #d8e8ff;
}
.task-meta {
    color: #c7c7c7;
    font-size: 11px;
}
.dismiss-button {
    color: #eeeeee;
    background: transparent;
    border: 0;
    box-shadow: none;
    padding: 2px 7px;
}
/* The hover tab at the right screen edge; only its left corners are rounded. */
.task-tab {
    background: rgba(35, 35, 35, 0.96);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-right-width: 0;
    border-radius: 10px 0 0 10px;
}
/* The label is rotated, so its top/bottom padding is the vertical breathing
   room of the project name. */
.task-tab label {
    color: #ffffff;
    font-weight: bold;
    font-size: 13px;
    padding: 10px 0;
}
/* Centered "new task" input opened by clicking the tab. */
.new-task-box {
    background: rgba(35, 35, 35, 0.98);
    border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 12px;
    padding: 10px;
}
.new-task-heading {
    color: #ffffff;
    font-weight: bold;
    font-size: 14px;
}
.new-task-status {
    color: #ffb4b4;
    font-size: 11px;
}
/* Vikunja priorities: 0 unset, 1 low, 2 medium, 3 high, 4 urgent, 5 DO NOW. */
.task-popup.priority-1, .task-tab.priority-1 {
    background: rgba(38, 58, 82, 0.96);
}
.task-popup.priority-2, .task-tab.priority-2 {
    background: rgba(40, 82, 52, 0.96);
}
.task-popup.priority-3, .task-tab.priority-3 {
    background: rgba(130, 96, 20, 0.96);
}
.task-popup.priority-4, .task-tab.priority-4 {
    background: rgba(160, 70, 20, 0.96);
}
.task-popup.priority-5, .task-tab.priority-5 {
    background: rgba(150, 25, 35, 0.96);
}
.task-popup.error-popup {
    background: rgba(120, 30, 30, 0.96);
}
"""

TAB_WIDTH = 28
# Minimum tab height; a tab grows with the length of its project name.
TAB_HEIGHT = 64
TAB_GAP = 8
# Longest project name (in characters) a tab shows before it is shortened, so
# one long name cannot push the other tabs off the screen.
TAB_MAX_CHARS = 26
# Grace period before hiding the panel, so the pointer can move between the
# tab and the panel without it closing.
HIDE_DELAY_MS = 300


def priority_class(priority: int) -> str | None:
    return f"priority-{min(priority, 5)}" if priority > 0 else None


def sort_key(task: VikunjaTask):
    # Higher Vikunja priorities first; tasks with due dates before tasks
    # without them, then stable by id.
    return (
        -task.priority,
        task.due_date is None,
        task.due_date or "",
        task.id,
    )


def setup_popup_window(
    window: Gtk.Window,
    config: Config,
    width: int | None = None,
    focusable: bool = False,
) -> None:
    """`focusable` windows can take keyboard focus when clicked (for entries),
    but still don't grab it when they are shown."""
    window.set_decorated(False)
    window.set_resizable(False)
    window.set_skip_taskbar_hint(True)
    window.set_skip_pager_hint(True)
    window.set_keep_above(True)
    window.set_accept_focus(focusable)
    window.set_focus_on_map(False)
    window.set_default_size(config.popup_width if width is None else width, -1)

    # An RGBA visual lets the transparent window background show the desktop
    # instead of the theme's window color around the card's rounded corners.
    screen = window.get_screen()
    visual = screen.get_rgba_visual()
    if visual is not None and screen.is_composited():
        window.set_visual(visual)
    window.get_style_context().add_class("popup-window")

    if GtkLayerShell is not None:
        GtkLayerShell.init_for_window(window)
        GtkLayerShell.set_layer(window, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_anchor(window, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(window, GtkLayerShell.Edge.RIGHT, True)
        GtkLayerShell.set_keyboard_mode(
            window,
            GtkLayerShell.KeyboardMode.ON_DEMAND if focusable else GtkLayerShell.KeyboardMode.NONE,
        )
        GtkLayerShell.set_exclusive_zone(window, -1)
    elif focusable:
        # Mutter never gives keyboard focus to NOTIFICATION windows. UTILITY
        # windows can be focused and are still kept out of Alt+Tab.
        window.set_type_hint(Gdk.WindowTypeHint.UTILITY)
    else:
        # Mutter keeps notification windows above others, out of Alt+Tab, and
        # below the top bar.
        window.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)


def place_popup_window(
    window: Gtk.Window,
    config: Config,
    top: int = 0,
    right: int | None = None,
) -> None:
    """Position a shown popup `top` pixels below the top margin and `right`
    pixels from the right screen edge (default: `config.margin_right`).

    Call after show_all(): before that, GTK reports a placeholder size.
    """
    if right is None:
        right = config.margin_right

    if GtkLayerShell is not None:
        GtkLayerShell.set_margin(window, GtkLayerShell.Edge.TOP, config.margin_top + top)
        GtkLayerShell.set_margin(window, GtkLayerShell.Edge.RIGHT, right)
        return

    width, _height = window.get_size()
    display = Gdk.Display.get_default()
    monitor = display.get_primary_monitor() or display.get_monitor(0)
    geometry = monitor.get_workarea()  # excludes the GNOME top bar
    x = geometry.x + geometry.width - width - right
    y = geometry.y + config.margin_top + top
    window.move(x, y)


def task_web_url(base_url: str, task_id: int) -> str:
    # base_url may point at the API ("https://host/api/v2"); the web UI lives
    # at the host root.
    web_base = re.sub(r"/api/v\d+/?$", "", base_url.rstrip("/"))
    return f"{web_base}/tasks/{task_id}"


def format_due_date(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        value = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        if dt.timestamp() < 0:
            return ''
        return dt.astimezone().strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return raw

class FilterCard(Gtk.EventBox):
    def __init__(self, config: Config, on_change) -> None:
        super().__init__()
        self.config = config
        self.on_change = on_change

        # Input-only: receives clicks without painting a theme background.
        self.set_visible_window(False)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        outer.get_style_context().add_class("task-popup")
        outer.set_border_width(10)
        self.add(outer)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.pack_start(row, False, False, 0)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Filter Tasks")
        self.entry.set_width_chars(40)
        self.entry.connect("changed", lambda *_: self.on_change())
        self.entry.connect("key-press-event", self._on_key)
        self.entry.connect("button-press-event", self._focus_entry)
        row.pack_start(self.entry, True, True, 0)

    def matches(self, task: VikunjaTask) -> bool:
        query = self.entry.get_text().strip().casefold()
        return not query or query in task.title.casefold()

    def _on_key(self, _entry, event) -> bool:
        # Escape clears the filter.
        if event.keyval == Gdk.KEY_Escape and self.entry.get_text():
            self.entry.set_text("")
            return True
        return False

    def _focus_entry(self, _entry, event) -> bool:
        # The panel doesn't take focus when shown; request it with the click's
        # timestamp so the window manager grants it.
        self.get_toplevel().present_with_time(event.time)
        self.entry.grab_focus()
        return False

class TaskCard(Gtk.EventBox):
    def __init__(
        self,
        task: VikunjaTask,
        config: Config,
        on_dismiss,
    ) -> None:
        super().__init__()
        self.task = task
        self.config = config
        self.on_dismiss = on_dismiss

        # Input-only: receives clicks without painting a theme background.
        self.set_visible_window(False)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        outer.get_style_context().add_class("task-popup")
        css_class = priority_class(self.task.priority)
        if css_class:
            outer.get_style_context().add_class(css_class)
        outer.set_border_width(10)
        self.add(outer)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.pack_start(row, False, False, 0)

        title = Gtk.Label()
        url = task_web_url(self.config.base_url, self.task.id)
        title.set_markup(
            f'<a href="{html.escape(url)}">{html.escape(self.task.title)}</a>'
        )
        title.set_tooltip_text(url)
        title.connect("activate-link", self._open_link)
        title.set_xalign(0)
        title.set_line_wrap(True)
        title.set_line_wrap_mode(2)
        title.set_ellipsize(3)
        title.set_max_width_chars(45)
        title.get_style_context().add_class("task-title")
        row.pack_start(title, True, True, 0)

        close = Gtk.Button(label="×")
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_tooltip_text("Hide this task")
        close.get_style_context().add_class("dismiss-button")
        close.connect("clicked", self._dismiss)
        row.pack_end(close, False, False, 0)

        meta_parts = []
        if self.task.priority:
            meta_parts.append(f"Priority {self.task.priority}")
        due = format_due_date(self.task.due_date)
        if due:
            meta_parts.append(f"Due {due}")

        if meta_parts:
            meta = Gtk.Label(label=" · ".join(meta_parts))
            meta.set_xalign(0)
            meta.get_style_context().add_class("task-meta")
            outer.pack_start(meta, False, False, 0)

        self.connect("button-press-event", self._handle_click)

    @staticmethod
    def _open_link(_label, uri: str) -> bool:
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error:
            LOG.exception("Could not open %s", uri)
        return True

    def _dismiss(self, *_args) -> None:
        self.on_dismiss(self.task.id)

    def _handle_click(self, _widget, event) -> bool:
        # Middle click is an additional quick-dismiss gesture.
        if event.button == 2:
            self._dismiss()
            return True
        return False


class TaskPanel(Gtk.Window):
    """One window holding all task cards; shown while hovering the tab."""

    def __init__(self, config: Config, on_filter_change) -> None:
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.config = config
        self.tasks: list[VikunjaTask] = []
        self._placed_size: tuple[int, int] | None = None

        self.set_name("task-panel-window")
        setup_popup_window(self, config, focusable=True)
        self.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=config.gap)
        self.add(box)
        # The filter is created once so its text and focus survive refreshes;
        # only the task cards below it are rebuilt.
        self.filter_card = FilterCard(config, on_filter_change)
        box.pack_start(self.filter_card, False, False, 0)
        self.cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=config.gap)
        box.pack_start(self.cards, False, False, 0)
        # The window shrinks/grows with its cards; keep it right-aligned.
        self.connect("size-allocate", self._on_size_allocate)

    def set_tasks(self, tasks: list[VikunjaTask], on_dismiss) -> None:
        self.tasks = list(tasks)
        for child in self.cards.get_children():
            child.destroy()

        for task in self.tasks:
            self.cards.pack_start(TaskCard(task, self.config, on_dismiss), False, False, 0)
        self.cards.show_all()
        self.resize(1, 1)

    def filter_has_focus(self) -> bool:
        return self.filter_card.entry.has_focus()

    def matches_filter(self, task: VikunjaTask) -> bool:
        return self.filter_card.matches(task)

    def show_panel(self) -> None:
        self.show_all()
        self.place()

    def place(self) -> None:
        self._placed_size = self.get_size()
        # Every project's panel opens at the same height, whichever tab it
        # belongs to.
        place_popup_window(self, self.config, right=TAB_WIDTH)

    def _on_size_allocate(self, *_args) -> None:
        if self.get_mapped() and self.get_size() != self._placed_size:
            self.place()


class TaskTab(Gtk.Window):
    """Small tab at the right screen edge showing one project's name."""

    def __init__(self, config: Config, project: VikunjaProject) -> None:
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.config = config
        self.project = project

        self.set_name("task-tab-window")
        setup_popup_window(self, config, width=TAB_WIDTH)
        self.add_events(
            Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
        )

        self.frame = Gtk.Box()
        # Only the minimum height is fixed: the rotated label makes the tab as
        # tall as the project name needs.
        self.frame.set_size_request(TAB_WIDTH, TAB_HEIGHT)
        self.frame.get_style_context().add_class("task-tab")
        self.label = Gtk.Label()
        # Rotated clockwise, so the name reads downwards along the screen edge.
        self.label.set_angle(270)
        self.label.set_hexpand(True)
        self.label.set_vexpand(True)
        self.frame.pack_start(self.label, True, True, 0)
        self.add(self.frame)

        self.set_project(project)

    def set_project(self, project: VikunjaProject) -> None:
        self.project = project
        # GTK cannot ellipsize a rotated label, so shorten the name here.
        title = project.title
        if len(title) > TAB_MAX_CHARS:
            title = title[: TAB_MAX_CHARS - 1].rstrip() + "…"
        self.label.set_text(title)

    def set_tasks(self, tasks: list[VikunjaTask]) -> None:
        self.set_tooltip_text(
            f"{self.project.title}: {len(tasks)} task(s) - click to add a task"
        )

        style = self.frame.get_style_context()
        for level in range(1, 6):
            style.remove_class(f"priority-{level}")
        css_class = priority_class(max((task.priority for task in tasks), default=0))
        if css_class:
            style.add_class(css_class)

    def show_tab(self, top: int) -> int:
        """Show the tab `top` pixels down the right edge; returns its height."""
        self.show_all()
        place_popup_window(self, self.config, top=top, right=0)
        return self.get_size()[1]


class NewTaskDialog(Gtk.Window):
    """Centered single-line input that creates a task in one project."""

    def __init__(self, config: Config, project: VikunjaProject, on_submit, on_close) -> None:
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.config = config
        self.project = project
        self.on_submit = on_submit
        self.on_close = on_close

        self.set_title("New Vikunja task")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self.set_visual(visual)
        self.get_style_context().add_class("popup-window")

        if GtkLayerShell is not None:
            # No anchors: the compositor centers the surface.
            GtkLayerShell.init_for_window(self)
            GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
            GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.EXCLUSIVE)
        else:
            self.set_type_hint(Gdk.WindowTypeHint.DIALOG)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(14)
        box.get_style_context().add_class("new-task-box")
        self.add(box)

        heading = Gtk.Label(label=f"New task in {project.title}")
        heading.set_xalign(0)
        heading.get_style_context().add_class("new-task-heading")
        box.pack_start(heading, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.pack_start(row, False, False, 0)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Task title")
        self.entry.set_width_chars(40)
        self.entry.connect("activate", self._submit)
        self.entry.connect("changed", self._update_button)
        row.pack_start(self.entry, True, True, 0)

        self.button = Gtk.Button(label="Create task")
        self.button.get_style_context().add_class("suggested-action")
        self.button.set_sensitive(False)
        self.button.connect("clicked", self._submit)
        row.pack_end(self.button, False, False, 0)

        self.status = Gtk.Label()
        self.status.set_xalign(0)
        self.status.set_line_wrap(True)
        self.status.get_style_context().add_class("new-task-status")
        self.status.set_no_show_all(True)  # only shown for errors
        box.pack_start(self.status, False, False, 0)

        self.connect("key-press-event", self._on_key)
        self.connect("destroy", lambda *_: self.on_close())

    def open(self, timestamp: int) -> None:
        self.show_all()
        if GtkLayerShell is None:
            width, height = self.get_size()
            display = Gdk.Display.get_default()
            monitor = display.get_primary_monitor() or display.get_monitor(0)
            area = monitor.get_workarea()
            self.move(area.x + (area.width - width) // 2, area.y + (area.height - height) // 2)
        # Using the click's timestamp lets the window manager give us focus.
        self.present_with_time(timestamp)
        self.entry.grab_focus()

    def set_busy(self, busy: bool) -> None:
        self.entry.set_sensitive(not busy)
        self.button.set_sensitive(not busy and bool(self.entry.get_text().strip()))
        self.button.set_label("Creating…" if busy else "Create task")

    def show_error(self, message: str) -> None:
        self.set_busy(False)
        self.status.set_text(f"Could not create task: {message}")
        self.status.show()
        self.entry.grab_focus()

    def _update_button(self, *_args) -> None:
        self.button.set_sensitive(bool(self.entry.get_text().strip()))

    def _submit(self, *_args) -> None:
        title = self.entry.get_text().strip()
        if not title or not self.entry.get_sensitive():
            return
        self.status.hide()
        self.set_busy(True)
        self.on_submit(title)

    def _on_key(self, _widget, event) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False


class MessagePopup(Gtk.Window):
    def __init__(self, text: str, config: Config) -> None:
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        setup_popup_window(self, config)

        box = Gtk.Box()
        box.set_border_width(12)
        box.get_style_context().add_class("task-popup")
        box.get_style_context().add_class("error-popup")
        label = Gtk.Label(label=text)
        label.set_line_wrap(True)
        label.set_xalign(0)
        box.add(label)
        self.add(box)

        GLib.timeout_add_seconds(8, self._expire)

    def _expire(self) -> bool:
        self.destroy()
        return False


class ProjectView:
    """One project: its tab, its panel and the task state behind them.

    Each project keeps its own dismissals, filter and hover state, so the
    projects never interfere with each other.
    """

    def __init__(self, project: VikunjaProject, config: Config, on_dismiss) -> None:
        self.project = project
        self.config = config
        self.on_dismiss = on_dismiss

        self.tasks: list[VikunjaTask] = []
        # Tasks the tab counts: not dismissed, capped at max_visible, ignoring
        # the filter.
        self.visible: list[VikunjaTask] = []
        self.dismissed: set[int] = set()

        self.tab = TaskTab(config, project)
        self.panel = TaskPanel(config, on_filter_change=self.render)
        self.inside = {"tab": False, "panel": False}
        self._hide_source: int | None = None

    def set_project(self, project: VikunjaProject) -> None:
        self.project = project
        self.tab.set_project(project)

    def set_tasks(self, tasks: list[VikunjaTask]) -> None:
        # If a task disappears from the active project list, forget its local
        # dismissal so it can reappear if it is later reopened.
        self.dismissed.intersection_update({task.id for task in tasks})
        self.tasks = tasks
        self.render()

    def render(self) -> None:
        active = [
            task
            for task in sorted(self.tasks, key=sort_key)
            if task.id not in self.dismissed
        ]
        self.visible = active[: self.config.max_visible]
        # The filter searches all active tasks, not just the first max_visible.
        shown = [task for task in active if self.panel.matches_filter(task)]
        self.panel.set_tasks(shown[: self.config.max_visible], self.dismiss)
        self.tab.set_tasks(self.visible)
        if not self.visible:
            # The panel stays open while a filter matches nothing, but not
            # while the project itself is empty.
            self.hide_panel(force=True)

    def dismiss(self, task_id: int) -> None:
        self.dismissed.add(task_id)
        # Re-render so the next task fills the slot, then let the app re-fetch
        # for fresh data.
        self.render()
        self.on_dismiss()

    def place(self, top: int) -> int:
        """Show the tab `top` pixels down the edge; returns its height."""
        return self.tab.show_tab(top)

    def show_panel(self) -> None:
        if not self.panel.get_visible() and self.visible:
            self.panel.show_panel()

    def hide_panel(self, force: bool = False) -> None:
        if force:
            self.cancel_hide()
            self.inside["panel"] = False
        self.panel.hide()

    def schedule_hide(self) -> None:
        if not any(self.inside.values()) and self._hide_source is None:
            self._hide_source = GLib.timeout_add(HIDE_DELAY_MS, self._hide_timeout)

    def cancel_hide(self) -> None:
        if self._hide_source is not None:
            GLib.source_remove(self._hide_source)
            self._hide_source = None

    def _hide_timeout(self) -> bool:
        self._hide_source = None
        # Keep the panel open while the user is typing in the filter.
        if not any(self.inside.values()) and not self.panel.filter_has_focus():
            self.panel.hide()
        return False

    def destroy(self) -> None:
        self.cancel_hide()
        self.panel.destroy()
        self.tab.destroy()


class VikunjaPopupApp:
    def __init__(self) -> None:
        self.config = load_config()
        self.client = VikunjaClient(
            self.config.base_url,
            self.config.token,
            verify_tls=self.config.verify_tls,
        )
        # One view per project, in the order the API returns them.
        self.views: dict[int, ProjectView] = {}
        self.fetching = False

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self.new_task_dialog: NewTaskDialog | None = None

    def start(self) -> None:
        signal.signal(signal.SIGINT, lambda *_: Gtk.main_quit())
        signal.signal(signal.SIGTERM, lambda *_: Gtk.main_quit())

        self.refresh()
        GLib.timeout_add_seconds(self.config.refresh_seconds, self._refresh_timer)
        Gtk.main()

    def _refresh_timer(self) -> bool:
        self.refresh()
        return True

    def refresh(self) -> None:
        if self.fetching:
            return
        self.fetching = True
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self) -> None:
        try:
            projects = self.client.get_projects()
            tasks: dict[int, list[VikunjaTask]] = {}
            errors: list[str] = []
            for project in projects:
                try:
                    tasks[project.id] = self.client.get_project_tasks(
                        project.id,
                        include_done=self.config.include_done,
                    )
                except Exception as exc:
                    # One unreadable project must not blank out the others;
                    # its tab keeps the tasks of the previous refresh.
                    LOG.exception("Failed to refresh project %s", project.id)
                    errors.append(f"{project.title}: {exc}")
            GLib.idle_add(self._apply_projects, projects, tasks, errors)
        except Exception as exc:
            LOG.exception("Failed to refresh Vikunja projects")
            GLib.idle_add(self._show_error, str(exc))
        finally:
            self.fetching = False

    def _apply_projects(
        self,
        projects: list[VikunjaProject],
        tasks: dict[int, list[VikunjaTask]],
        errors: list[str],
    ) -> bool:
        # Rebuilt in API order, so the tabs follow the project order and views
        # of projects that are gone are dropped.
        views: dict[int, ProjectView] = {}
        for project in projects:
            view = self.views.pop(project.id, None)
            if view is None:
                view = ProjectView(project, self.config, on_dismiss=self.refresh)
                self._connect_view(view)
            else:
                view.set_project(project)
            views[project.id] = view
            if project.id in tasks:
                view.set_tasks(tasks[project.id])

        for gone in self.views.values():
            gone.destroy()
        self.views = views

        self._place_tabs()
        if errors:
            self._show_error("\n".join(errors))
        return False

    def _connect_view(self, view: ProjectView) -> None:
        # Hover state: a panel stays open while the pointer is inside its tab
        # or itself, and hides HIDE_DELAY_MS after it has left both.
        for name, window in (("tab", view.tab), ("panel", view.panel)):
            window.connect("enter-notify-event", self._on_enter, view, name)
            window.connect("leave-notify-event", self._on_leave, view, name)
        view.tab.connect("button-press-event", self._on_tab_click, view)
        view.panel.connect("focus-out-event", self._on_panel_focus_out, view)

    def _place_tabs(self) -> None:
        # The tabs stack downwards from the top right corner; each one is as
        # tall as its rotated project name needs.
        top = 0
        for view in self.views.values():
            top += view.place(top) + TAB_GAP

    def _on_tab_click(self, _widget, event, view: ProjectView) -> bool:
        if event.button != 1:
            return False
        view.hide_panel(force=True)
        # A dialog for another project is replaced rather than left behind.
        if self.new_task_dialog is not None:
            self.new_task_dialog.destroy()
        self.new_task_dialog = NewTaskDialog(
            self.config,
            view.project,
            on_submit=self._create_task,
            on_close=self._on_dialog_closed,
        )
        self.new_task_dialog.open(event.time)
        return True

    def _on_dialog_closed(self) -> None:
        self.new_task_dialog = None

    def _create_task(self, title: str) -> None:
        dialog = self.new_task_dialog
        if dialog is None:
            return
        project_id = dialog.project.id

        def worker() -> None:
            try:
                self.client.create_task(project_id, title)
            except Exception as exc:
                LOG.exception("Failed to create Vikunja task")
                GLib.idle_add(self._on_task_create_failed, dialog, str(exc))
            else:
                GLib.idle_add(self._on_task_created, dialog)

        threading.Thread(target=worker, daemon=True).start()

    def _on_task_created(self, dialog: NewTaskDialog | None) -> bool:
        if dialog is not None and dialog is self.new_task_dialog:
            dialog.destroy()
        self._refresh_soon()
        return False

    def _refresh_soon(self) -> bool:
        # refresh() is a no-op while a fetch is running; retry until it runs
        # so the new task shows up without waiting for the refresh timer.
        if self.fetching:
            GLib.timeout_add(500, self._refresh_soon)
        else:
            self.refresh()
        return False

    def _on_task_create_failed(self, dialog: NewTaskDialog | None, message: str) -> bool:
        if dialog is not None and dialog is self.new_task_dialog:
            dialog.show_error(message)
        return False

    def _on_enter(self, _window, _event, view: ProjectView, name: str) -> bool:
        view.inside[name] = True
        view.cancel_hide()
        if name == "tab":
            # Only one panel at a time: moving to another tab closes the rest
            # right away instead of waiting out their hide delay.
            for other in self.views.values():
                if other is not view:
                    other.hide_panel(force=True)
            view.show_panel()
        return False

    def _on_leave(self, _window, event, view: ProjectView, name: str) -> bool:
        # Moving onto a child with its own input window (button, card) sends
        # a leave with detail INFERIOR while the pointer is still inside.
        if event.detail == Gdk.NotifyType.INFERIOR:
            return False
        view.inside[name] = False
        view.schedule_hide()
        return False

    def _on_panel_focus_out(self, _window, _event, view: ProjectView) -> bool:
        # The panel stayed open while the filter had focus; hide it now if
        # the pointer is elsewhere.
        view.schedule_hide()
        return False

    def _show_error(self, message: str) -> bool:
        popup = MessagePopup(
            f"Vikunja refresh failed:\n{message}\n\nConfig: {CONFIG_FILE}",
            self.config,
        )
        popup.show_all()
        place_popup_window(popup, self.config, right=TAB_WIDTH)
        return False


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        app = VikunjaPopupApp()
        app.start()
    except Exception as exc:
        print(f"vikunja-popups: {exc}")
        print(f"Configuration file: {CONFIG_FILE}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
