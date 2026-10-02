#!/usr/bin/env bash
# Install or update vikunja-popups for the current user.
# Safe to re-run: it updates the code, keeps the config and restarts the
# service if it is running.
set -euo pipefail

APP_NAME="vikunja-popups"
APP_DIR="${HOME}/.local/share/${APP_NAME}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/${APP_NAME}"
SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE="${APP_NAME}.service"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="${APP_NAME}-settings.desktop"
ICON_FILE="${APP_NAME}-settings.svg"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

APP_FILES=(app.py app_control.py ai_mail.py config.py github_client.py mail_client.py opencode_models.py settings.py vikunja_client.py requirements.txt
  vikunja-popups-settings.svg)

if [[ "${EUID}" -eq 0 ]]; then
  echo "Run this script as your normal user, not with sudo." >&2
  echo "It asks for sudo itself when system packages are missing." >&2
  exit 1
fi

# --- System packages --------------------------------------------------------
# PyGObject and GTK come from apt; pip only installs requests.
packages=(python3 python3-gi gir1.2-gtk-3.0)

# venv needs ensurepip, which may come from python3-venv or python3.X-venv.
if ! python3 -c 'import ensurepip' 2>/dev/null; then
  packages+=(python3-venv)
fi

# GNOME (Mutter) does not support wlr-layer-shell; app.py uses XWayland there.
# Other Wayland compositors (Sway, Hyprland, KDE, ...) use gtk-layer-shell.
if [[ "${XDG_CURRENT_DESKTOP:-}" != *GNOME* ]]; then
  packages+=(gir1.2-gtklayershell-0.1)
fi

missing=()
for pkg in "${packages[@]}"; do
  if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
    missing+=("$pkg")
  fi
done

if ((${#missing[@]})); then
  echo "Installing Ubuntu packages: ${missing[*]}"
  sudo apt-get update
  sudo apt-get install -y "${missing[@]}"
else
  echo "Ubuntu packages already installed."
fi

# --- Application ------------------------------------------------------------
echo "Installing application to $APP_DIR ..."
mkdir -p "$APP_DIR" "$CONFIG_DIR" "$SERVICE_DIR"
for file in "${APP_FILES[@]}"; do
  install -m 0644 "$SOURCE_DIR/$file" "$APP_DIR/$file"
done
rm -rf "$APP_DIR/__pycache__"

# --system-site-packages makes the apt-installed PyGObject visible in the venv.
# Recreate the venv when the system Python version changed.
venv_python="$APP_DIR/.venv/bin/python"
if [[ -x "$venv_python" ]] && "$venv_python" -c 'import sys' 2>/dev/null \
  && [[ "$("$venv_python" -c 'import sys; print(sys.version_info[:2])')" \
     == "$(python3 -c 'import sys; print(sys.version_info[:2])')" ]]; then
  echo "Reusing virtual environment."
else
  echo "Creating virtual environment..."
  rm -rf "$APP_DIR/.venv"
  python3 -m venv --system-site-packages "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check \
  -r "$APP_DIR/requirements.txt"

if ! "$venv_python" -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' 2>/dev/null; then
  echo "Error: GTK 3 bindings are not importable from $venv_python" >&2
  exit 1
fi

# --- Configuration ----------------------------------------------------------
if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
  install -m 0600 "$SOURCE_DIR/config.example.json" "$CONFIG_DIR/config.json"
  created_config=1
else
  chmod 0600 "$CONFIG_DIR/config.json"
  created_config=0
fi

# --- systemd user service ---------------------------------------------------
install -m 0644 "$SOURCE_DIR/$SERVICE" "$SERVICE_DIR/$SERVICE"
systemctl --user daemon-reload

if systemctl --user is-enabled --quiet "$SERVICE" 2>/dev/null; then
  # Re-enable so the unit is linked to its current [Install] target.
  systemctl --user reenable "$SERVICE" >/dev/null 2>&1
fi

if systemctl --user is-active --quiet "$SERVICE"; then
  systemctl --user restart "$SERVICE"
  echo "Restarted $SERVICE."
fi

# --- Settings app in the application menu -----------------------------------
# .desktop files do not expand ~ or variables, so fill in the absolute path.
mkdir -p "$DESKTOP_DIR"
sed "s|@APP_DIR@|$APP_DIR|g" "$SOURCE_DIR/$DESKTOP_FILE.in" > "$DESKTOP_DIR/$DESKTOP_FILE"
chmod 0644 "$DESKTOP_DIR/$DESKTOP_FILE"
mkdir -p "$ICON_DIR"
install -m 0644 "$SOURCE_DIR/$ICON_FILE" "$ICON_DIR/$ICON_FILE"
# GTK trusts an existing icon cache over the directory, so refresh it.
icon_theme_dir="$(dirname "$(dirname "$ICON_DIR")")"
if [[ -f "$icon_theme_dir/icon-theme.cache" ]] && command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache --quiet --force --ignore-theme-index "$icon_theme_dir" || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi

echo
echo "Installed."
if ((created_config)); then
  echo "Created $CONFIG_DIR/config.json (mode 0600) - check base_url and token."
fi
echo "Settings:  open \"Vikunja Popups Settings\" from the application menu"
echo "Test:      $venv_python $APP_DIR/app.py"
if ! systemctl --user is-enabled --quiet "$SERVICE" 2>/dev/null; then
  echo "Autostart: systemctl --user enable --now $SERVICE"
fi
echo "Logs:      journalctl --user -u $SERVICE -f"
