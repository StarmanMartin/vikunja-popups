#!/usr/bin/env bash
set -euo pipefail

systemctl --user disable --now vikunja-popups.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/vikunja-popups.service"
systemctl --user daemon-reload
rm -rf "$HOME/.local/share/vikunja-popups"
rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/vikunja-popups-settings.desktop"
rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps/vikunja-popups-settings.svg"

echo "Application removed."
echo "Configuration was kept at:"
echo "  ${XDG_CONFIG_HOME:-$HOME/.config}/vikunja-popups"
