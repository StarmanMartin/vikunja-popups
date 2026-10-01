from __future__ import annotations

import html
import logging
import os
import re
import signal
import threading
import time
from dataclasses import asdict
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

import ai_mail  # noqa: E402
import github_client  # noqa: E402
import mail_client  # noqa: E402
from config import CONFIG_FILE, Config, load_config, load_state, save_state  # noqa: E402
from vikunja_client import (  # noqa: E402
    VikunjaClient,
    VikunjaProject,
    VikunjaTask,
    text_to_html,
)


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
/* Email proposals from the AI. */
.task-tab.ai-tab {
    background: rgba(86, 52, 140, 0.96);
}
.proposal-text, .proposal-card check label {
    color: #e8e8e8;
}
/* The scrolling container of the proposal panel paints nothing itself. */
#proposal-panel-window scrolledwindow, #proposal-panel-window viewport {
    background: transparent;
}
.proposal-error {
    color: #ffb4b4;
    font-size: 11px;
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
# Emails handed to the AI per check; the rest follow on the next refresh.
MAIL_BATCH = 10
# The same for GitHub items (assigned issues/PRs, new comments on own PRs).
GITHUB_BATCH = 10


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


class PanelWindow(Gtk.Window):
    """A window of cards left of the tab column, shown while hovering a tab."""

    def __init__(self, config: Config) -> None:
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.config = config
        self._placed_size: tuple[int, int] | None = None

        setup_popup_window(self, config, focusable=True)
        self.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        # The window shrinks/grows with its cards; keep it right-aligned.
        self.connect("size-allocate", self._on_size_allocate)

    def show_panel(self) -> None:
        self.show_all()
        self.place()

    def place(self) -> None:
        self._placed_size = self.get_size()
        # Every panel opens at the same height, whichever tab it belongs to.
        place_popup_window(self, self.config, right=TAB_WIDTH)

    def _on_size_allocate(self, *_args) -> None:
        if self.get_mapped() and self.get_size() != self._placed_size:
            self.place()


class TaskPanel(PanelWindow):
    """One window holding all task cards of a project."""

    def __init__(self, config: Config, on_filter_change) -> None:
        super().__init__(config)
        self.tasks: list[VikunjaTask] = []
        self.set_name("task-panel-window")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=config.gap)
        self.add(box)
        # The filter is created once so its text and focus survive refreshes;
        # only the task cards below it are rebuilt.
        self.filter_card = FilterCard(config, on_filter_change)
        box.pack_start(self.filter_card, False, False, 0)
        self.cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=config.gap)
        box.pack_start(self.cards, False, False, 0)

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


class TaskTab(Gtk.Window):
    """Small tab at the right screen edge showing a project's name."""

    def __init__(self, config: Config, heading: str) -> None:
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.config = config
        self.heading = heading

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

        self.set_heading(heading)

    def set_heading(self, heading: str) -> None:
        self.heading = heading
        # GTK cannot ellipsize a rotated label, so shorten the name here.
        if len(heading) > TAB_MAX_CHARS:
            heading = heading[: TAB_MAX_CHARS - 1].rstrip() + "…"
        self.label.set_text(heading)

    def set_tasks(self, tasks: list[VikunjaTask]) -> None:
        self.set_tooltip_text(
            f"{self.heading}: {len(tasks)} task(s) - click to add a task"
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


def wrapped_label(text: str, css_class: str = "proposal-text") -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.set_xalign(0)
    label.set_line_wrap(True)
    label.set_line_wrap_mode(2)  # WORD_CHAR
    label.set_max_width_chars(50)
    label.get_style_context().add_class(css_class)
    return label


def focus_on_click(widget: Gtk.Widget) -> None:
    # Panels don't take focus when shown; request it with the click's
    # timestamp so the window manager grants it to the editor.
    def on_press(_widget, event) -> bool:
        widget.get_toplevel().present_with_time(event.time)
        return False

    widget.connect("button-press-event", on_press)


def text_editor(text: str, lines: int) -> tuple[Gtk.ScrolledWindow, Gtk.TextView]:
    view = Gtk.TextView()
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    view.set_left_margin(6)
    view.set_right_margin(6)
    view.get_buffer().set_text(text)
    focus_on_click(view)
    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroller.set_min_content_height(lines * 18)
    scroller.set_shadow_type(Gtk.ShadowType.IN)
    scroller.add(view)
    return scroller, view


def text_of(view: Gtk.TextView) -> str:
    buffer = view.get_buffer()
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False).strip()


def describe_action(action: dict, task_titles: dict[int, str], project_titles: dict[int, str]) -> str:
    kind = action["type"]
    if kind == "create_task":
        text = f"Create a task in {project_titles.get(action['project_id'], action['project_id'])}"
        extras = []
        if action.get("priority"):
            extras.append(f"priority {action['priority']}")
        if action.get("due_date"):
            extras.append(f"due {format_due_date(action['due_date'])}")
        return text + (f" ({', '.join(extras)})" if extras else "")
    task = task_titles.get(action.get("task_id"), f"task #{action.get('task_id')}")
    if kind == "add_comment":
        return f"Comment on “{task}”"
    if kind == "reply":
        return "Send the reply"
    lines =[f"Change “{task}”:"]
    changes = action["changes"]
    if "title" in changes:
        lines.append(f"• title → {changes['title']}")
    if "priority" in changes:
        lines.append(f"• priority → {changes['priority']}")
    if "due_date" in changes:
        lines.append(f"• due → {format_due_date(changes['due_date'])}")
    if changes.get("done"):
        lines.append("• mark as done")
    if "append_description" in changes:
        lines.append(f"• add to the description: {changes['append_description']}")
    return "\n".join(lines)


class ProposalCard(Gtk.EventBox):
    """What the AI proposes for one email or GitHub item; nothing happens until confirmed."""

    def __init__(
        self,
        proposal: dict,
        task_titles: dict[int, str],
        project_titles: dict[int, str],
        on_execute,
        on_discard,
    ) -> None:
        super().__init__()
        self.proposal = proposal
        self.on_execute = on_execute
        self.on_discard = on_discard
        # (check button, function returning the action with the user's edits)
        self.rows: list[tuple[Gtk.CheckButton, object]] = []

        self.set_visible_window(False)
        self._build_ui(task_titles, project_titles)

    def _build_ui(self, task_titles: dict[int, str], project_titles: dict[int, str]) -> None:
        message = self.proposal["message"]
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.get_style_context().add_class("task-popup")
        outer.get_style_context().add_class("proposal-card")
        outer.set_border_width(10)
        self.add(outer)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.pack_start(row, False, False, 0)
        title = wrapped_label(message.get("subject") or "(no subject)", "task-title")
        if message.get("url"):
            title.set_markup(
                f'<a href="{html.escape(message["url"])}">{html.escape(title.get_text())}</a>'
            )
            title.set_tooltip_text(message["url"])
            title.connect("activate-link", TaskCard._open_link)
        row.pack_start(title, True, True, 0)
        close = Gtk.Button(label="×")
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_tooltip_text("Discard these proposals without doing anything")
        close.get_style_context().add_class("dismiss-button")
        close.connect("clicked", lambda *_: self.on_discard(self.proposal["id"]))
        row.pack_end(close, False, False, 0)

        received = datetime.fromtimestamp(message.get("timestamp") or 0).strftime("%d.%m.%Y %H:%M")
        outer.pack_start(wrapped_label(f"{message.get('sender', '')} · {received}", "task-meta"), False, False, 0)
        if self.proposal.get("summary"):
            outer.pack_start(wrapped_label(self.proposal["summary"]), False, False, 0)
        related = [task_titles.get(i, f"task #{i}") for i in self.proposal.get("related_task_ids", [])]
        if related:
            outer.pack_start(wrapped_label("Belongs to: " + ", ".join(related), "task-meta"), False, False, 0)
        if self.proposal.get("error"):
            outer.pack_start(wrapped_label(self.proposal["error"], "proposal-error"), False, False, 0)

        for action in self.proposal.get("actions", []):
            self._add_action(outer, action, task_titles, project_titles)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.pack_start(bottom, False, False, 4)
        self.status = wrapped_label("", "proposal-error")
        bottom.pack_start(self.status, True, True, 0)
        self.button = Gtk.Button(label="Execute selected")
        self.button.get_style_context().add_class("suggested-action")
        self.button.connect("clicked", self._execute)
        self.button.set_no_show_all(not self.rows)
        bottom.pack_end(self.button, False, False, 0)

    def _add_action(self, box: Gtk.Box, action: dict, task_titles, project_titles) -> None:
        text = describe_action(action, task_titles, project_titles)
        if action["type"] == "reply":
            text = f"Send this reply to {self.proposal['message'].get('reply_to', '?')}"
        check = Gtk.CheckButton(label=text)
        check.set_active(True)
        check_label = check.get_child()
        check_label.set_line_wrap(True)
        check_label.set_max_width_chars(50)
        check.connect("toggled", self._update_button)
        box.pack_start(check, False, False, 0)

        editors = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        editors.set_margin_start(24)
        kind = action["type"]
        if kind == "create_task":
            title = Gtk.Entry()
            title.set_text(action["title"])
            title.set_placeholder_text("Task title")
            focus_on_click(title)
            editors.pack_start(title, False, False, 0)
            scroller, description = text_editor(action.get("description", ""), 3)
            editors.pack_start(scroller, False, False, 0)

            def read() -> dict:
                return {**action, "title": title.get_text().strip(), "description": text_of(description)}
        elif kind in ("add_comment", "reply"):
            field = "comment" if kind == "add_comment" else "body"
            scroller, view = text_editor(action[field], 3 if kind == "add_comment" else 8)
            editors.pack_start(scroller, False, False, 0)

            def read() -> dict:
                return {**action, field: text_of(view)}
        else:
            def read() -> dict:
                return dict(action)

        if editors.get_children():
            box.pack_start(editors, False, False, 0)
            check.connect("toggled", lambda button: editors.set_sensitive(button.get_active()))
        self.rows.append((check, read))

    def _selected(self) -> list[dict]:
        return [read() for check, read in self.rows if check.get_active()]

    def _update_button(self, *_args) -> None:
        self.button.set_sensitive(any(check.get_active() for check, _read in self.rows))

    def _execute(self, *_args) -> None:
        actions = self._selected()
        for action in actions:
            if (action["type"] == "create_task" and not action["title"]) or (
                action["type"] in ("add_comment", "reply")
                and not action["comment" if action["type"] == "add_comment" else "body"]
            ):
                self.status.set_text("Fill in the empty field or untick that action.")
                return
        self.on_execute(self, self.proposal["id"], actions)

    def set_busy(self, busy: bool) -> None:
        self.set_sensitive(not busy)
        self.button.set_label("Executing…" if busy else "Execute selected")
        if busy:
            self.status.set_text("")


class ProposalPanel(PanelWindow):
    """The cards of all emails and GitHub items the AI has proposals for."""

    def __init__(self, config: Config, on_execute, on_discard) -> None:
        super().__init__(config)
        self.on_execute = on_execute
        self.on_discard = on_discard
        self.set_name("proposal-panel-window")
        self.by_id: dict[str, ProposalCard] = {}

        # Many cards may not fit on the screen.
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_propagate_natural_height(True)
        self.scroller.set_propagate_natural_width(True)
        self.scroller.set_min_content_width(config.popup_width)
        self.cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=config.gap)
        self.scroller.add(self.cards)
        self.add(self.scroller)

    def _card(self, proposal: dict, task_titles, project_titles) -> ProposalCard:
        return ProposalCard(proposal, task_titles, project_titles, self.on_execute, self.on_discard)

    def set_proposals(self, proposals: list[dict], task_titles, project_titles) -> None:
        # Existing cards are kept, so edits in them survive new arrivals.
        ids = {proposal["id"] for proposal in proposals}
        for proposal_id in list(self.by_id):
            if proposal_id not in ids:
                self.by_id.pop(proposal_id).destroy()
        for proposal in proposals:
            if proposal["id"] not in self.by_id:
                card = self._card(proposal, task_titles, project_titles)
                self.by_id[proposal["id"]] = card
                self.cards.pack_start(card, False, False, 0)
        self._fit()

    def replace(self, proposal: dict, task_titles, project_titles) -> None:
        old = self.by_id.get(proposal["id"])
        card = self._card(proposal, task_titles, project_titles)
        self.cards.pack_start(card, False, False, 0)
        if old is not None:
            self.cards.reorder_child(card, self.cards.get_children().index(old))
            old.destroy()
        self.by_id[proposal["id"]] = card
        self._fit()

    def _fit(self) -> None:
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        height = monitor.get_workarea().height - self.config.margin_top - 24
        self.scroller.set_max_content_height(max(200, height))
        self.cards.show_all()
        self.resize(1, 1)


class HoverView:
    """A tab and the panel it reveals while the pointer is over either."""

    def __init__(self, tab: TaskTab, panel: PanelWindow) -> None:
        self.tab = tab
        self.panel = panel
        self.inside = {"tab": False, "panel": False}
        self._hide_source: int | None = None

    def can_show(self) -> bool:
        return True

    def keep_open(self) -> bool:
        """True while the panel must stay open although the pointer left."""
        return False

    def place(self, top: int) -> int:
        """Show the tab `top` pixels down the edge; returns its height."""
        return self.tab.show_tab(top)

    def show_panel(self) -> None:
        if not self.panel.get_visible() and self.can_show():
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
        if not any(self.inside.values()) and not self.keep_open():
            self.panel.hide()
        return False

    def destroy(self) -> None:
        self.cancel_hide()
        self.panel.destroy()
        self.tab.destroy()


class ProjectView(HoverView):
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

        super().__init__(
            TaskTab(config, project.title),
            TaskPanel(config, on_filter_change=self.render),
        )

    def set_project(self, project: VikunjaProject) -> None:
        self.project = project
        self.tab.set_heading(project.title)

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

    def can_show(self) -> bool:
        return bool(self.visible)

    def keep_open(self) -> bool:
        # Keep the panel open while the user is typing in the filter.
        return self.panel.filter_has_focus()


class AiView(HoverView):
    """The "Inbox" tab: AI proposals for new emails and GitHub items, waiting for confirmation."""

    def __init__(self, config: Config, on_execute, on_discard) -> None:
        self.proposals: list[dict] = []
        tab = TaskTab(config, "Inbox")
        tab.frame.get_style_context().add_class("ai-tab")
        super().__init__(tab, ProposalPanel(config, on_execute, on_discard))

    def set_proposals(self, proposals: list[dict], task_titles, project_titles) -> None:
        self.proposals = proposals
        self.panel.set_proposals(proposals, task_titles, project_titles)
        self.tab.set_heading(f"Inbox · {len(proposals)}")
        self.tab.set_tooltip_text(f"AI proposals for {len(proposals)} email(s) or GitHub item(s)")
        if not proposals:
            self.hide_panel(force=True)
            self.tab.hide()

    def can_show(self) -> bool:
        return bool(self.proposals)

    def keep_open(self) -> bool:
        # Stay open while the user edits a proposal; focus-out hides it.
        return self.panel.is_active()


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

        # Email + AI: the last handled email and the proposals waiting for
        # confirmation live in the state file, so they survive restarts.
        self.projects: list[VikunjaProject] = []
        self.state = load_state()
        self.mail_busy = False
        self._mail_started = float("-inf")
        self._last_mail_error: str | None = None
        self.github_busy = False
        self._github_started = float("-inf")
        self._last_github_error: str | None = None
        self.ai_view = AiView(
            self.config,
            on_execute=self._execute_proposal,
            on_discard=self._discard_proposal,
        )
        self._connect_hover(self.ai_view)

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
                self._connect_hover(view)
                view.tab.connect("button-press-event", self._on_tab_click, view)
            else:
                view.set_project(project)
            views[project.id] = view
            if project.id in tasks:
                view.set_tasks(tasks[project.id])

        for gone in self.views.values():
            gone.destroy()
        self.views = views
        self.projects = projects

        self._update_ai_view()
        self._place_tabs()
        if errors:
            self._show_error("\n".join(errors))
        self._start_mail_check()
        self._start_github_check()
        return False

    def _connect_hover(self, view: HoverView) -> None:
        # Hover state: a panel stays open while the pointer is inside its tab
        # or itself, and hides HIDE_DELAY_MS after it has left both.
        for name, window in (("tab", view.tab), ("panel", view.panel)):
            window.connect("enter-notify-event", self._on_enter, view, name)
            window.connect("leave-notify-event", self._on_leave, view, name)
        view.panel.connect("focus-out-event", self._on_panel_focus_out, view)

    def _all_views(self) -> list[HoverView]:
        return [self.ai_view, *self.views.values()]

    def _place_tabs(self) -> None:
        # The tabs stack downwards from the top right corner; each one is as
        # tall as its rotated project name needs. The Inbox tab comes first
        # and only exists while there are proposals.
        top = 0
        for view in self._all_views():
            if view is self.ai_view and not self.ai_view.proposals:
                continue
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

    def _on_enter(self, _window, _event, view: HoverView, name: str) -> bool:
        view.inside[name] = True
        view.cancel_hide()
        if name == "tab":
            # Only one panel at a time: moving to another tab closes the rest
            # right away instead of waiting out their hide delay.
            for other in self._all_views():
                if other is not view:
                    other.hide_panel(force=True)
            view.show_panel()
        return False

    def _on_leave(self, _window, event, view: HoverView, name: str) -> bool:
        # Moving onto a child with its own input window (button, card) sends
        # a leave with detail INFERIOR while the pointer is still inside.
        if event.detail == Gdk.NotifyType.INFERIOR:
            return False
        view.inside[name] = False
        view.schedule_hide()
        return False

    def _on_panel_focus_out(self, _window, _event, view: HoverView) -> bool:
        # The panel stayed open while the filter had focus; hide it now if
        # the pointer is elsewhere.
        view.schedule_hide()
        return False

    def _show_error(self, message: str, heading: str = "Vikunja refresh failed") -> bool:
        popup = MessagePopup(
            f"{heading}:\n{message}\n\nConfig: {CONFIG_FILE}",
            self.config,
        )
        popup.show_all()
        place_popup_window(popup, self.config, right=TAB_WIDTH)
        return False

    # --- Email + AI ---------------------------------------------------------
    def _proposals(self) -> list[dict]:
        proposals = self.state.get("proposals")
        if not isinstance(proposals, list):
            proposals = self.state["proposals"] = []
        return proposals

    def _find_proposal(self, proposal_id: str) -> dict | None:
        return next((p for p in self._proposals() if p.get("id") == proposal_id), None)

    def _save_state(self) -> None:
        try:
            save_state(self.state)
        except OSError:
            LOG.exception("Could not save the state file")

    def _titles(self) -> tuple[dict[int, str], dict[int, str]]:
        task_titles = {task.id: task.title for view in self.views.values() for task in view.tasks}
        return task_titles, {project.id: project.title for project in self.projects}

    def _update_ai_view(self) -> None:
        self.ai_view.set_proposals(list(self._proposals()), *self._titles())

    def _start_mail_check(self) -> None:
        """Hand new emails to the AI, once per refresh interval."""
        if self.mail_busy or not self.projects:
            return
        if not (self.config.ai_model and self.config.email.imap_host):
            return
        # Dismissing or creating a task also refreshes; don't poll the mail
        # server for each of those.
        now = time.monotonic()
        if now - self._mail_started < self.config.refresh_seconds / 2:
            return
        self._mail_started = now

        since = self.state.get("mail_since")
        if not isinstance(since, (int, float)):
            # First run: start from now instead of handing the whole inbox to
            # the AI.
            self.state["mail_since"] = time.time()
            self.state["mail_seen"] = []
            self._save_state()
            LOG.info("Email: starting with emails that arrive from now on")
            return

        self.mail_busy = True
        tasks = {project_id: list(view.tasks) for project_id, view in self.views.items()}
        threading.Thread(
            target=self._mail_worker,
            args=(float(since), set(self.state.get("mail_seen") or []), list(self.projects), tasks),
            daemon=True,
        ).start()

    def _mail_worker(
        self,
        since: float,
        seen: set[str],
        projects: list[VikunjaProject],
        tasks: dict[int, list[VikunjaTask]],
    ) -> None:
        # Runs in a thread: no GTK calls here.
        try:
            messages = mail_client.fetch_new_messages(self.config.email, since, seen, MAIL_BATCH)
            for message in messages:
                result = ai_mail.analyze(
                    self.config.ai_model, message, self.config.email.address, projects, tasks
                )
                GLib.idle_add(self._on_mail_analyzed, message, result)
            GLib.idle_add(self._on_mail_done, None)
        except Exception as exc:
            # The timestamp only moves past emails the AI has answered, so the
            # failed one is tried again next time.
            LOG.exception("Email check failed")
            GLib.idle_add(self._on_mail_done, str(exc))

    def _on_mail_analyzed(self, message: mail_client.MailMessage, result: dict) -> bool:
        since = self.state.get("mail_since") or 0
        if message.timestamp > since:
            self.state["mail_since"] = message.timestamp
            self.state["mail_seen"] = [message.key]
        else:
            # Same second as the previous email: remember it by key.
            self.state.setdefault("mail_seen", []).append(message.key)

        if result["actions"] or result["error"]:
            self._proposals().append({"id": message.key, "message": asdict(message), **result})
        else:
            LOG.info("Email %r needs nothing: %s", message.subject, result["summary"])
        self._save_state()
        self._update_ai_view()
        self._place_tabs()
        return False

    def _on_mail_done(self, error: str | None) -> bool:
        self.mail_busy = False
        # Show a failure once, not on every refresh while it persists.
        if error and error != self._last_mail_error:
            self._show_error(error, heading="Email check failed")
        self._last_mail_error = error
        return False

    # --- GitHub + AI --------------------------------------------------------
    def _start_github_check(self) -> None:
        """Hand new GitHub items to the AI, once per refresh interval.

        State: `github_assigned` holds the refs of open assigned issues/PRs
        that were already handled, `github_since` the start of the window for
        new comments on the user's PRs and `github_seen` the comment items
        handled within the current window.
        """
        github = self.config.github
        if self.github_busy or not self.projects:
            return
        if not (self.config.ai_model and github.token):
            return
        now = time.monotonic()
        if now - self._github_started < self.config.refresh_seconds / 2:
            return
        self._github_started = now

        assigned = self.state.get("github_assigned")
        since = self.state.get("github_since")
        first_run = not isinstance(assigned, list) or not isinstance(since, (int, float))
        self.github_busy = True
        tasks = {project_id: list(view.tasks) for project_id, view in self.views.items()}
        threading.Thread(
            target=self._github_worker,
            args=(
                first_run,
                set(assigned or []),
                float(since or 0),
                set(self.state.get("github_seen") or []),
                list(self.projects),
                tasks,
            ),
            daemon=True,
        ).start()

    def _github_worker(
        self,
        first_run: bool,
        assigned_seen: set[str],
        since: float,
        seen: set[str],
        projects: list[VikunjaProject],
        tasks: dict[int, list[VikunjaTask]],
    ) -> None:
        # Runs in a thread: no GTK calls here.
        account = self.config.github
        try:
            login = account.username or github_client.get_login(account)
            client = github_client.GitHubClient(account)
            until = time.time()
            assigned = client.assigned(login)
            open_refs = [item.ref for item in assigned]
            if first_run:
                # Start from now instead of handing everything to the AI.
                GLib.idle_add(self._on_github_done, None, open_refs, until)
                return
            items = [item for item in assigned if item.ref not in assigned_seen]
            items += [item for item in client.new_pr_comments(login, since, until) if item.key not in seen]
            for item in items[:GITHUB_BATCH]:
                result = ai_mail.analyze_github(self.config.ai_model, item, projects, tasks)
                GLib.idle_add(self._on_github_analyzed, item, result)
            # With items left over, the comment window stays open for the next check.
            complete = len(items) <= GITHUB_BATCH
            GLib.idle_add(self._on_github_done, None, open_refs, until if complete else None)
        except Exception as exc:
            # Nothing unanswered is marked as handled, so it is tried again.
            LOG.exception("GitHub check failed")
            GLib.idle_add(self._on_github_done, str(exc), None, None)

    def _on_github_analyzed(self, item: github_client.GitHubItem, result: dict) -> bool:
        handled = self.state.setdefault(
            "github_assigned" if item.kind == "assigned" else "github_seen", []
        )
        mark = item.ref if item.kind == "assigned" else item.key
        if mark not in handled:
            handled.append(mark)

        if (result["actions"] or result["error"]) and self._find_proposal(item.key) is None:
            what = "Assigned to you" if item.kind == "assigned" else f"New comments by {item.author}"
            message = {
                "source": "github",
                "subject": f"{item.ref}: {item.title}",
                "sender": f"GitHub · {what}",
                "timestamp": item.timestamp,
                "url": item.url,
            }
            self._proposals().append({"id": item.key, "message": message, **result})
        else:
            LOG.info("GitHub %s needs nothing: %s", item.ref, result["summary"])
        self._save_state()
        self._update_ai_view()
        self._place_tabs()
        return False

    def _on_github_done(self, error: str | None, open_refs: list[str] | None, until: float | None) -> bool:
        self.github_busy = False
        if open_refs is not None:
            # Forget issues/PRs that are closed or no longer assigned, so one
            # that is assigned again later is handled again.
            if not isinstance(self.state.get("github_assigned"), list):
                self.state["github_assigned"] = list(open_refs)
                LOG.info("GitHub: starting with items that appear from now on")
            else:
                still_open = set(open_refs)
                self.state["github_assigned"] = [
                    ref for ref in self.state["github_assigned"] if ref in still_open
                ]
        if until is not None:
            self.state["github_since"] = until
            self.state["github_seen"] = []
        if open_refs is not None or until is not None:
            self._save_state()
        if error and error != self._last_github_error:
            self._show_error(error, heading="GitHub check failed")
        self._last_github_error = error
        return False

    def _discard_proposal(self, proposal_id: str) -> None:
        self.state["proposals"] = [p for p in self._proposals() if p.get("id") != proposal_id]
        self._save_state()
        self._update_ai_view()
        self._place_tabs()

    def _execute_proposal(self, card: ProposalCard, proposal_id: str, actions: list[dict]) -> None:
        proposal = self._find_proposal(proposal_id)
        if proposal is None:
            return
        card.set_busy(True)
        message = proposal["message"]
        task_titles, project_titles = self._titles()

        def worker() -> None:
            failed: list[dict] = []
            errors: list[str] = []
            for action in actions:
                try:
                    self._run_action(action, message)
                except Exception as exc:
                    LOG.exception("Proposal action failed: %s", action["type"])
                    failed.append(action)
                    label = describe_action(action, task_titles, project_titles).splitlines()[0]
                    errors.append(f"{label}: {exc}")
            GLib.idle_add(self._on_proposal_executed, proposal_id, failed, errors)

        threading.Thread(target=worker, daemon=True).start()

    def _run_action(self, action: dict, message: dict) -> None:
        # Runs in a thread: no GTK calls here.
        kind = action["type"]
        if kind == "create_task":
            self.client.create_task(
                action["project_id"],
                action["title"],
                description=text_to_html(action.get("description", "")),
                priority=action.get("priority", 0),
                due_date=action.get("due_date"),
            )
        elif kind == "update_task":
            changes = dict(action["changes"])
            append = changes.pop("append_description", None)
            if append:
                current = self.client.get_task(action["task_id"]).description
                changes["description"] = current + text_to_html(append)
            self.client.update_task(action["task_id"], changes)
        elif kind == "add_comment":
            self.client.add_comment(action["task_id"], text_to_html(action["comment"]))
        elif kind == "reply":
            mail_client.send_reply(self.config.email, message, action["body"])
        else:
            raise ValueError(f"Unknown action {kind!r}")

    def _on_proposal_executed(self, proposal_id: str, failed: list[dict], errors: list[str]) -> bool:
        proposal = self._find_proposal(proposal_id)
        if proposal is not None:
            if failed:
                # Keep only what failed (with the user's edits) for another
                # try; unticked actions were declined and are dropped.
                proposal["actions"] = failed
                proposal["error"] = "\n".join(errors)
                self.ai_view.panel.replace(proposal, *self._titles())
            else:
                self._proposals().remove(proposal)
            self._save_state()
        self._update_ai_view()
        self._place_tabs()
        self._refresh_soon()
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
