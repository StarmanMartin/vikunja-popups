from __future__ import annotations

import os
import subprocess
import threading

import gi

gi.require_version("Gtk", "3.0")

from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

from config import CONFIG_FILE, EXAMPLE, read_raw_config, save_raw_config
from vikunja_client import VikunjaClient


PRGNAME = "vikunja-popups-settings"
SERVICE = "vikunja-popups.service"

# key, label, lower bound (the same bounds load_config() clamps to)
NUMBER_FIELDS = (
    ("refresh_seconds", "Refresh interval (seconds)", 10),
    ("max_visible", "Max. cards per project", 1),
    ("popup_width", "Popup width", 260),
    ("margin_top", "Top margin", 0),
    ("margin_right", "Right margin", 0),
    ("gap", "Gap between cards", 0),
)
SWITCH_FIELDS = (
    ("include_done", "Show completed tasks"),
    ("verify_tls", "Verify TLS certificate"),
)


class SettingsWindow(Gtk.Window):
    def __init__(self) -> None:
        super().__init__(title="Vikunja Popups Settings")
        self.set_border_width(18)
        self.set_default_size(520, -1)
        self.set_icon_name("preferences-system")
        self.connect("destroy", Gtk.main_quit)

        self.data = read_raw_config()
        self.testing = False

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        row = 0

        self.base_url = Gtk.Entry(hexpand=True)
        self.base_url.set_placeholder_text(EXAMPLE["base_url"])
        self.base_url.set_text(str(self.data.get("base_url", "")))
        row = self._add_row(grid, row, "Vikunja URL", self.base_url)

        self.token = Gtk.Entry(hexpand=True)
        self.token.set_visibility(False)
        self.token.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.token.set_icon_from_icon_name(
            Gtk.EntryIconPosition.SECONDARY, "view-reveal-symbolic"
        )
        self.token.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "Show or hide")
        self.token.connect("icon-press", self._on_token_icon)
        self.token.set_text(str(self.data.get("token", "")))
        row = self._add_row(grid, row, "API token", self.token)

        if os.environ.get("VIKUNJA_TOKEN"):
            note = Gtk.Label(
                label="VIKUNJA_TOKEN is set in the environment and overrides this token.",
                xalign=0,
                wrap=True,
            )
            note.get_style_context().add_class("dim-label")
            grid.attach(note, 1, row, 1, 1)
            row += 1

        self.spins: dict[str, Gtk.SpinButton] = {}
        for key, label, lower in NUMBER_FIELDS:
            spin = Gtk.SpinButton.new_with_range(lower, 100000, 1)
            spin.set_halign(Gtk.Align.START)
            spin.set_value(self._int_value(key, lower))
            self.spins[key] = spin
            row = self._add_row(grid, row, label, spin)

        self.switches: dict[str, Gtk.Switch] = {}
        for key, label in SWITCH_FIELDS:
            switch = Gtk.Switch(halign=Gtk.Align.START)
            switch.set_active(bool(self.data.get(key, EXAMPLE[key])))
            self.switches[key] = switch
            row = self._add_row(grid, row, label, switch)

        self.status = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.status.set_text(str(CONFIG_FILE))
        self.status.get_style_context().add_class("dim-label")

        self.test_button = Gtk.Button(label="Test connection")
        self.test_button.connect("clicked", self._on_test)
        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda *_: self.destroy())
        save_button = Gtk.Button(label="Save")
        save_button.get_style_context().add_class("suggested-action")
        save_button.connect("clicked", self._on_save)

        buttons = Gtk.Box(spacing=8)
        buttons.pack_start(self.test_button, False, False, 0)
        buttons.pack_end(save_button, False, False, 0)
        buttons.pack_end(close_button, False, False, 0)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.pack_start(grid, True, True, 0)
        box.pack_start(self.status, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        self.add(box)

    @staticmethod
    def _add_row(grid: Gtk.Grid, row: int, text: str, widget: Gtk.Widget) -> int:
        label = Gtk.Label(label=text, xalign=0)
        grid.attach(label, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)
        return row + 1

    def _int_value(self, key: str, lower: int) -> int:
        try:
            return max(lower, int(self.data.get(key, EXAMPLE[key])))
        except (TypeError, ValueError):
            return max(lower, EXAMPLE[key])

    def _on_token_icon(self, entry: Gtk.Entry, *_args) -> None:
        entry.set_visibility(not entry.get_visibility())

    def _set_status(self, text: str) -> None:
        self.status.set_text(text)

    def _form_values(self) -> dict:
        values = {
            "base_url": self.base_url.get_text().strip(),
            "token": self.token.get_text().strip(),
        }
        for key, spin in self.spins.items():
            values[key] = spin.get_value_as_int()
        for key, switch in self.switches.items():
            values[key] = switch.get_active()
        return values

    def _check_base_url(self, values: dict) -> bool:
        if not values["base_url"] or values["base_url"] == EXAMPLE["base_url"]:
            self._set_status("Enter the URL of your Vikunja server.")
            self.base_url.grab_focus()
            return False
        return True

    # --- Test connection ----------------------------------------------------
    def _on_test(self, _button: Gtk.Button) -> None:
        values = self._form_values()
        if self.testing or not self._check_base_url(values):
            return
        token = os.environ.get("VIKUNJA_TOKEN", values["token"]).strip()
        if not token:
            self._set_status("Enter an API token.")
            self.token.grab_focus()
            return

        self.testing = True
        self.test_button.set_sensitive(False)
        self._set_status("Connecting...")
        threading.Thread(
            target=self._test_worker,
            args=(values["base_url"], token, values["verify_tls"]),
            daemon=True,
        ).start()

    def _test_worker(self, base_url: str, token: str, verify_tls: bool) -> None:
        # Runs in a thread: no GTK calls here.
        try:
            client = VikunjaClient(base_url, token, verify_tls=verify_tls)
            count = len(client.get_projects())
            text = f"Connection OK: {count} project{'' if count == 1 else 's'} found."
        except Exception as exc:
            text = f"Connection failed: {exc}"
        GLib.idle_add(self._test_done, text)

    def _test_done(self, text: str) -> bool:
        self.testing = False
        self.test_button.set_sensitive(True)
        self._set_status(text)
        return False

    # --- Save ---------------------------------------------------------------
    def _on_save(self, _button: Gtk.Button) -> None:
        values = self._form_values()
        if not self._check_base_url(values):
            return
        # Update the dict that was read, so keys unknown to this form are kept.
        self.data.update(values)
        try:
            save_raw_config(self.data)
        except OSError as exc:
            self._set_status(f"Could not save: {exc}")
            return
        self._set_status(f"Saved. {self._restart_service()}")

    @staticmethod
    def _restart_service() -> str:
        try:
            active = subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", SERVICE],
                timeout=10,
            ).returncode == 0
            if not active:
                return "The popup service is not running; start it to apply the settings."
            result = subprocess.run(
                ["systemctl", "--user", "restart", SERVICE],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"Could not restart the popup service: {exc}"
        if result.returncode != 0:
            return f"Could not restart the popup service: {result.stderr.strip()}"
        return "Popup service restarted."


def main() -> None:
    # Matches the .desktop file name so the desktop shows its name and icon.
    GLib.set_prgname(PRGNAME)
    window = SettingsWindow()
    window.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
