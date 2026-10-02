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
- Optional email assistant: an AI model (via opencode) reads new emails and
  proposes task changes and replies, which you confirm before they happen

## Install

```bash
./install.sh
```

Run it as your normal user (it calls `sudo` only if Ubuntu packages are
missing). Re-run it after changing the code: it updates the installed files,
keeps your configuration and restarts the service if it is running.

Then open **Vikunja Popups Settings** from the application menu, enter the
server URL and API token, and save. Saving restarts the popup service if it is
running. "Test connection" checks the URL and token without saving. "Stop app"
stops the popup service until the next login; while it is stopped, the same
button reads "Start app".

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
  "verify_tls": true,
  "ai_model": "",
  "email": {
    "address": "",
    "username": "",
    "password": "",
    "imap_host": "",
    "imap_port": 993,
    "imap_security": "ssl",
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_security": "starttls"
  },
  "github": {
    "username": "",
    "token": "",
    "api_url": "https://api.github.com"
  }
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
- `ai_model`: an [opencode](https://opencode.ai) model id in the form
  `provider/model`, or empty for none. The settings app offers every model
  that `opencode models` lists. Used for the email assistant (below).
- `email`: one email account (the "Email" tab of the settings app). `username`
  may stay empty when it equals `address`. `imap_security` and
  `smtp_security` are `ssl`, `starttls` or `none`. The password is stored in
  the config file (mode `0600`), like the API token. "Test login" tries the
  IMAP and SMTP login without saving.
- `github`: one GitHub account (the "GitHub" tab of the settings app):
  a personal access `token` and `api_url` (change it only for GitHub
  Enterprise, e.g. `https://ghe.example.com/api/v3`). `username` cannot be
  edited: the settings app looks it up from the token when it is saved. The token is
  stored in the config file (mode `0600`). "Test login" checks the token
  without saving; "Create token" opens GitHub's page for a new fine-grained
  token. Used by the GitHub assistant (below).

## Email assistant

Active when both `ai_model` and an IMAP server are set.

- On every refresh the app reads the emails that arrived in the INBOX since
  the last one it handled (by the server's arrival time). Emails are opened
  read-only and are not marked as read. The very first time it only stores
  the current time, so old emails are never processed.
- Each new email goes to the AI together with your projects and open tasks.
  The AI decides whether the email belongs to existing tasks, whether a task
  should be created or changed (title, priority, due date, done, text added
  to the description, a comment), and whether it needs an answer, which it
  drafts.
- New tasks always go to a project called **ToDo**. If you don't have one,
  it is created the first time such a task is executed.
- Emails with proposals get a card in the purple **Inbox** tab at the top of
  the tab column. Each proposed action has a checkbox; task titles, texts,
  comments and the reply can be edited. Nothing happens until you click
  "Execute selected"; `×` discards the card. Actions that fail stay on the card
  with the error, so you can try again.
- "Ask again…" on a card opens a box in the middle of the screen where you can
  give the AI additional instructions, such as "Please write in English" or
  "It should be a new task". "Send to AI" (or Ctrl+Enter) sends the email
  together with the previous proposal and your instructions back to the AI;
  its new answer replaces the card. If that fails, the old proposal stays.
- Replies go to the sender (or `Reply-To`) from your `address` via SMTP, as an
  answer in the same thread with the original quoted. They are not copied to a
  Sent folder.
- At most 10 emails are handled per refresh. If the AI cannot be reached, the
  same email is tried again on the next refresh.
- The AI runs through `opencode run --standalone` with its own agent
  (`vikunja-popups-mail`) that may not use any tools, so text in an email
  cannot make it run commands or change files. Each email creates an opencode
  session. Email text and your task list are sent to the model's provider.
- The last handled email and the open proposals are kept in
  `~/.local/state/vikunja-popups/state.json` (mode `0600`), so they survive
  restarts. Delete the file to start over from "now".

## GitHub assistant

Active when both `ai_model` and a GitHub token are set.

- On every refresh the app asks GitHub for
  - open issues and pull requests assigned to you that it has not handled
    yet, and
  - new comments and reviews by others on pull requests you opened (issue
    comments, review comments, and reviews that approve, request changes or
    have text), one item per pull request.
  The very first time it only remembers what is already assigned and the
  current time, so old items are never processed.
- Each item goes to the AI together with your projects and open tasks. The AI
  says whether it belongs to an existing task (a comment, and changes to the
  task if needed) or needs a new task. The link to GitHub goes into the
  comment or description. New tasks go to the **ToDo** project, as with
  emails.
- Proposals appear in the **Inbox** tab like the email ones; the card title
  links to GitHub. GitHub items get no reply action. "Ask again…" works the
  same way (not for cards stored by an older version of the app).
- An issue or pull request that is closed or unassigned is forgotten, so it is
  handled again if it is assigned to you again later.
- At most 10 items are handled per refresh; if the AI cannot be reached they
  are tried again. The GitHub texts and your task list are sent to the
  model's provider. The token needs read access to issues and pull requests
  of the repositories involved.

## Remove

```bash
./uninstall.sh
```
