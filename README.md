# Vikunja Popups

Small Ubuntu desktop application that fetches the tasks of every Vikunja
project and displays them as dismissible top-right popup cards.

## Features

- Vikunja REST API v2
- Fetches every page of tasks from every project the token can see
- Shows unfinished tasks by default
- Stays out of the way: one narrow tab per project at the right screen edge,
  stacked from the top and labelled with the project name written downwards;
  hovering a tab reveals that project's task cards, which hide again shortly
  after the mouse leaves the tab and the cards
- Click a tab to open a centered input and create a new task in that project
  (Enter or the button creates it, Escape closes)
- Dismiss button (`×`) and middle-click dismiss
- Refreshes periodically
- Dismissals last while the process is running
- GNOME (Wayland or X11): runs through XWayland so the popups can be positioned
- Other Wayland compositors: uses `gtk-layer-shell` (installed automatically
  when the desktop is not GNOME)
- Background color by task priority, task title links to the Vikunja web UI
- Optional systemd user autostart with the graphical session
- API token can be kept out of the config with `VIKUNJA_TOKEN`

## Install

```bash
./install.sh
```

Run it as your normal user (it calls `sudo` only if Ubuntu packages are
missing). Re-run it after changing the code: it updates the installed files,
keeps your configuration and restarts the service if it is running.

Then open **Vikunja Popups Settings** from the application menu, enter the
server URL and API token, and save. Saving restarts the popup service if it is
running. "Test connection" checks the URL and token without saving.

You can also edit the file by hand:

```text
~/.config/vikunja-popups/config.json
```

Example:

```json
{
  "base_url": "https://vikunja.example.com",
  "token": "tk_your_token_here",
  "refresh_seconds": 60,
  "include_done": false,
  "popup_width": 380,
  "margin_top": 18,
  "margin_right": 18,
  "gap": 10,
  "max_visible": 12,
  "verify_tls": true
}
```

The file is created with mode `0600`.

For better secret handling, leave `"token": ""` and launch it with:

```bash
export VIKUNJA_TOKEN='tk_...'
~/.local/share/vikunja-popups/.venv/bin/python \
  ~/.local/share/vikunja-popups/app.py
```

For systemd, you can put the variable into an environment file and add an
`EnvironmentFile=` line to the service, or use the mode-0600 config file.

## Test manually

```bash
~/.local/share/vikunja-popups/.venv/bin/python \
  ~/.local/share/vikunja-popups/app.py
```

## Start automatically at login

```bash
systemctl --user enable --now vikunja-popups.service
```

Logs:

```bash
journalctl --user -u vikunja-popups.service -f
```

Restart after changing the configuration by hand (the settings app does this
itself):

```bash
systemctl --user restart vikunja-popups.service
```

## Vikunja token

Create an API token in Vikunja's settings. It needs permission to list
projects and to read tasks in them.

## Dismiss behavior

Clicking `×` hides the task for the lifetime of the running application.
A dismissed task is forgotten if it disappears from the active task list
(for example because it is completed) so that it can appear again if it is
later reopened.

A project's tab stays visible when it has no tasks left, so you can still add
new ones.

Restarting the application clears all dismissals.

## Configuration

- `base_url`: your Vikunja base URL. Both `https://host` and
  `https://host/api/v2` are accepted. Every non-archived project the token can
  see gets a tab; there is nothing to configure per project.
- `refresh_seconds`: refresh interval, minimum 10 seconds.
- `include_done`: whether completed tasks should be displayed.
- `popup_width`: popup width.
- `margin_top`, `margin_right`: screen edge margins.
- `max_visible`: maximum number of task cards shown at once, per project.
- `verify_tls`: set to `false` only for development/self-signed setups where
  you intentionally do not want certificate verification.

## Remove

```bash
./uninstall.sh
```
