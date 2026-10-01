from __future__ import annotations

import os
import subprocess
import threading

import gi

gi.require_version("Gtk", "3.0")

from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

from config import (
    CONFIG_FILE,
    EXAMPLE,
    email_config_from,
    normalize_email,
    read_raw_config,
    save_raw_config,
)
from mail_client import test_login
from opencode_models import list_models
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


EMAIL_TEXT_FIELDS = (
    ("address", "Email address", ""),
    ("username", "Username", "Same as the email address"),
)
# prefix, label
EMAIL_SERVERS = (
    ("imap", "IMAP (incoming)"),
    ("smtp", "SMTP (outgoing)"),
)
EMAIL_SECURITY_LABELS = (
    ("ssl", "SSL/TLS"),
    ("starttls", "STARTTLS"),
    ("none", "None"),
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

        grid = Gtk.Grid(column_spacing=12, row_spacing=10, border_width=14)
        row = 0

        self.base_url = Gtk.Entry(hexpand=True)
        self.base_url.set_placeholder_text(EXAMPLE["base_url"])
        self.base_url.set_text(str(self.data.get("base_url", "")))
        row = self._add_row(grid, row, "Vikunja URL", self.base_url)

        self.token = self._secret_entry(str(self.data.get("token", "")))
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

        # The entry keeps the saved model usable when opencode is unavailable.
        self.ai_model = Gtk.ComboBoxText.new_with_entry()
        self.ai_model.set_hexpand(True)
        ai_entry = self.ai_model.get_child()
        ai_entry.set_placeholder_text("None")
        ai_entry.set_text(str(self.data.get("ai_model") or ""))
        row = self._add_row(grid, row, "AI model", self.ai_model)

        self.ai_note = Gtk.Label(
            label="Loading models from opencode...", xalign=0, wrap=True, max_width_chars=60
        )
        self.ai_note.get_style_context().add_class("dim-label")
        grid.attach(self.ai_note, 1, row, 1, 1)
        row += 1
        threading.Thread(target=self._models_worker, daemon=True).start()

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

        # max_width_chars keeps a long message from widening the window.
        self.status = Gtk.Label(xalign=0, wrap=True, selectable=True, max_width_chars=60)
        self.status.set_text(str(CONFIG_FILE))
        self.status.get_style_context().add_class("dim-label")

        self.test_button = Gtk.Button(label="Test connection")
        self.test_button.connect("clicked", self._on_test)
        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda *_: self.destroy())
        save_button = Gtk.Button(label="Save")
        save_button.get_style_context().add_class("suggested-action")
        save_button.connect("clicked", self._on_save)

        self.service_button = Gtk.Button()
        self.service_button.set_tooltip_text(
            "Stops or starts the installed popup service. "
            "A copy started with python app.py is not affected."
        )
        self.service_button.connect("clicked", self._on_service_toggle)

        buttons = Gtk.Box(spacing=8)
        buttons.pack_start(self.test_button, False, False, 0)
        buttons.pack_start(self.service_button, False, False, 0)
        buttons.pack_end(save_button, False, False, 0)
        buttons.pack_end(close_button, False, False, 0)

        notebook = Gtk.Notebook()
        notebook.append_page(grid, Gtk.Label(label="General"))
        notebook.append_page(self._build_email_page(), Gtk.Label(label="Email"))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.pack_start(notebook, True, True, 0)
        box.pack_start(self.status, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        self.add(box)
        self._update_service_button()

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

    @staticmethod
    def _secret_entry(text: str) -> Gtk.Entry:
        entry = Gtk.Entry(hexpand=True)
        entry.set_visibility(False)
        entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.SECONDARY, "view-reveal-symbolic"
        )
        entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "Show or hide")
        entry.connect(
            "icon-press", lambda e, *_args: e.set_visibility(not e.get_visibility())
        )
        entry.set_text(text)
        return entry

    def _build_email_page(self) -> Gtk.Grid:
        # One account only, stored as the "email" object of the config.
        email = normalize_email(self.data.get("email"))
        grid = Gtk.Grid(column_spacing=12, row_spacing=10, border_width=14)
        row = 0

        self.email_entries: dict[str, Gtk.Entry] = {}
        for key, label, placeholder in EMAIL_TEXT_FIELDS:
            entry = Gtk.Entry(hexpand=True)
            entry.set_placeholder_text(placeholder)
            entry.set_text(email[key])
            self.email_entries[key] = entry
            row = self._add_row(grid, row, label, entry)

        self.email_entries["password"] = self._secret_entry(email["password"])
        row = self._add_row(grid, row, "Password", self.email_entries["password"])

        self.email_ports: dict[str, Gtk.SpinButton] = {}
        self.email_security: dict[str, Gtk.ComboBoxText] = {}
        for prefix, label in EMAIL_SERVERS:
            host = Gtk.Entry(hexpand=True)
            host.set_placeholder_text(f"{prefix}.example.com")
            host.set_text(email[f"{prefix}_host"])
            self.email_entries[f"{prefix}_host"] = host

            port = Gtk.SpinButton.new_with_range(1, 65535, 1)
            port.set_value(email[f"{prefix}_port"])
            self.email_ports[f"{prefix}_port"] = port

            security = Gtk.ComboBoxText()
            for value, text in EMAIL_SECURITY_LABELS:
                security.append(value, text)
            security.set_active_id(email[f"{prefix}_security"])
            self.email_security[f"{prefix}_security"] = security

            row = self._add_row(grid, row, f"{label} server", host)
            details = Gtk.Box(spacing=8)
            details.pack_start(port, False, False, 0)
            details.pack_start(security, False, False, 0)
            row = self._add_row(grid, row, "Port and security", details)

        self.email_test_button = Gtk.Button(label="Test login", halign=Gtk.Align.START)
        self.email_test_button.connect("clicked", self._on_email_test)
        grid.attach(self.email_test_button, 1, row, 1, 1)
        return grid

    def _email_values(self) -> dict:
        # Start from what was read, so keys unknown to this form are kept.
        raw = self.data.get("email")
        email = dict(raw) if isinstance(raw, dict) else {}
        for key, entry in self.email_entries.items():
            text = entry.get_text()
            email[key] = text if key == "password" else text.strip()
        for key, spin in self.email_ports.items():
            email[key] = spin.get_value_as_int()
        for key, combo in self.email_security.items():
            email[key] = combo.get_active_id()
        return email

    def _set_status(self, text: str) -> None:
        self.status.set_text(text)

    def _form_values(self) -> dict:
        values = {
            "base_url": self.base_url.get_text().strip(),
            "token": self.token.get_text().strip(),
            "ai_model": self.ai_model.get_child().get_text().strip(),
            "email": self._email_values(),
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

    # --- AI models ----------------------------------------------------------
    def _models_worker(self) -> None:
        # Runs in a thread: no GTK calls here.
        try:
            models, error = list_models(), None
        except RuntimeError as exc:
            models, error = [], str(exc)
        GLib.idle_add(self._models_done, models, error)

    def _models_done(self, models: list[str], error: str | None) -> bool:
        for model in models:
            self.ai_model.append_text(model)
        if error:
            self.ai_note.set_text(f"{error}. You can still type a provider/model id.")
        elif not models:
            self.ai_note.set_text("opencode lists no models. You can still type a provider/model id.")
        else:
            self.ai_note.set_text(f"{len(models)} models from opencode. Leave empty for none.")
        return False

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

    # --- Test email login ---------------------------------------------------
    def _on_email_test(self, button: Gtk.Button) -> None:
        button.set_sensitive(False)
        self._set_status("Logging in to the mail server...")
        threading.Thread(
            target=self._email_test_worker,
            args=(self._email_values(),),
            daemon=True,
        ).start()

    def _email_test_worker(self, email: dict) -> None:
        # Runs in a thread: no GTK calls here.
        try:
            text = test_login(email_config_from(email))
        except RuntimeError as exc:
            text = str(exc)
        GLib.idle_add(self._email_test_done, text)

    def _email_test_done(self, text: str) -> bool:
        self.email_test_button.set_sensitive(True)
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
        self._update_service_button()

    # --- Service ------------------------------------------------------------
    @staticmethod
    def _service_active() -> bool:
        try:
            return subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", SERVICE],
                timeout=10,
            ).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _update_service_button(self) -> None:
        label = "Stop app" if self._service_active() else "Start app"
        self.service_button.set_label(label)

    def _on_service_toggle(self, _button: Gtk.Button) -> None:
        stop = self._service_active()
        try:
            result = subprocess.run(
                ["systemctl", "--user", "stop" if stop else "start", SERVICE],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._set_status(f"Could not {'stop' if stop else 'start'} the popup app: {exc}")
        else:
            if result.returncode != 0:
                self._set_status(
                    f"Could not {'stop' if stop else 'start'} the popup app: "
                    f"{result.stderr.strip()}"
                )
            elif stop:
                self._set_status("Popup app stopped (it starts again at next login).")
            else:
                self._set_status("Popup app started.")
        self._update_service_button()

    @classmethod
    def _restart_service(cls) -> str:
        try:
            if not cls._service_active():
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
