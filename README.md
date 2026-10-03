# Vikunja Popups

A small Ubuntu desktop app that keeps your [Vikunja](https://vikunja.io) tasks
at the edge of your screen, plus an **AI assistant** that reads your new
emails and GitHub activity and proposes what to do about them: new tasks,
changes and comments on existing tasks, and drafted email replies. Nothing
happens until you confirm it.

## AI assistant at a glance

- **Reads your inbox for you.** Every new email goes to an AI model together
  with your projects and open tasks. The AI recognises which task an email
  belongs to, proposes a new task when needed, proposes changes (title,
  priority, due date, done, text added to the description, a comment) and
  drafts a reply when the email needs an answer.
- **Watches GitHub.** Issues and pull requests assigned to you, and new
  comments and reviews on your own pull requests, become proposals too:
  a comment on the matching task, or a new task with the GitHub link.
- **You stay in control.** All proposals collect in the purple **Inbox** tab.
  Each action has a checkbox; titles, descriptions, comments and replies can
  be edited before you click "Execute selected".
- **Ask again.** Not happy with a proposal? "Ask again…" opens a box where
  you tell the AI what to change ("Please write in English", "It should be a
  new task"); its new answer replaces the card.
- **One place for new tasks.** Tasks the AI creates always go to your
  **ToDo** project, which is created automatically if it doesn't exist.
- **Any model.** The AI runs through [opencode](https://opencode.ai), so you
  can pick any model opencode supports (cloud or local) in the settings app.
  Email and GitHub texts and your task list are sent to that model's
  provider, so choose a local model for sensitive mail.
- **Safe by design.** The AI has no tools: it cannot run commands, read
  files or send anything. Instructions hidden in an email or GitHub text are
  ignored. Emails are opened read-only and are not marked as read.

Details: [Inbox and proposals](#inbox-and-proposals),
[Email assistant](#email-assistant), [GitHub assistant](#github-assistant).

## Task popups

- One narrow tab per Vikunja project at the right screen edge, stacked from
  the top and labelled with the project name written downwards; the tab's
  color shows the project's highest task priority
- Hovering a tab reveals that project's unfinished tasks as cards, sorted by
  priority and due date; they hide again shortly after the mouse leaves the
  tab and the cards
- A filter field at the top of the cards searches the project's task titles
  (Escape clears it)
- Card color by task priority; the task title links to the Vikunja web UI
- Dismiss a card with `×` or a middle click
- Click a tab to open a centered input and create a new task in that project
  (Enter or the button creates it, Escape closes)
- Refreshes periodically; every non-archived project the token can see gets
  a tab, nothing to configure per project
- Vikunja REST API v2, all pages of projects and tasks
- **Vikunja Popups Settings** app in the application menu: server, token,
  layout, AI model, email account and GitHub account, with "Test connection" /
  "Test login" buttons and a "Stop app" / "Start app" button
- GNOME (Wayland or X11): runs through XWayland so the popups can be
  positioned. Other Wayland compositors: uses `gtk-layer-shell` (installed
  automatically when the desktop is not GNOME)
- Runs on Ubuntu (and other Linux desktops) and on Windows 10/11 via MSYS2
- Optional autostart at login (systemd user service on Linux, Startup
  shortcut on Windows)
- API token can be kept out of the config with `VIKUNJA_TOKEN`

## Install

On Windows, see [Install on Windows](#install-on-windows).

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

## Install on Windows

The app uses GTK 3, which on Windows comes from [MSYS2](https://www.msys2.org),
a free collection of open-source tools and libraries for Windows.

1. Install MSYS2 with its installer from msys2.org (default folder
   `C:\msys64`).
2. In PowerShell, in this folder:

   ```powershell
   powershell -ExecutionPolicy Bypass -File install.ps1
   ```

   It installs GTK 3, PyGObject and requests with MSYS2's `pacman` (updating
   MSYS2 first when packages are missing), copies the app to
   `%LOCALAPPDATA%\Programs\vikunja-popups`, and creates the Start-menu
   entries **Vikunja Popups** and **Vikunja Popups Settings** plus a shortcut
   in the Startup folder so the app starts at login. Options:
   `-Msys2Root D:\msys64` when MSYS2 is elsewhere, `-NoAutostart` to skip
   the Startup shortcut.
3. Open **Vikunja Popups Settings**, enter the server URL and token, and save;
   then start **Vikunja Popups** (or use "Start app" in the settings).

Re-run `install.ps1` after updating the code; it keeps the configuration and
restarts the app if it is running.

### opencode on Windows

The AI assistant needs a standalone `opencode` CLI. The CLI bundled inside
the OpenCode desktop app does not help: it is not on the `PATH`, so the
settings app cannot list models. The app looks for `opencode` on the
`PATH`, in `~\.opencode\bin` and in npm's global folder.

A way that needs no package manager is to put the CLI into
`~\.opencode\bin` yourself. In PowerShell:

```powershell
$dir = "$env:TEMP\opencode-cli"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
# The metadata gives the latest version of the CLI.
$meta = Invoke-RestMethod 'https://opencode.ai/update/api/latest/cli/npm'
Invoke-WebRequest -Uri `
  "https://registry.npmjs.org/@opencode/cli-windows-x64/-/cli-windows-x64-$($meta.version).tgz" `
  -OutFile "$dir\opencode.tgz"
tar -xzf "$dir\opencode.tgz" -C $dir
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.opencode\bin" | Out-Null
Copy-Item "$dir\package\bin\opencode.exe" "$env:USERPROFILE\.opencode\bin\opencode.exe" -Force
& "$env:USERPROFILE\.opencode\bin\opencode.exe" --version
& "$env:USERPROFILE\.opencode\bin\opencode.exe" models
```

`opencode models` should list model ids such as `provider/model`. Then log in
to a model provider with `opencode auth login` and pick the model in
**Vikunja Popups Settings**. If the settings app was already open, close and
reopen it: it loads the model list only at startup.

Keep the CLI on the same major version as the OpenCode desktop app, if you
use it. Both share the data directory `~\.local\share\opencode`; a CLI of an
older generation cannot read the newer database and fails with

```text
Database is not empty and has no session table
```

If that happens, install the CLI version that matches the desktop app
(v2: `curl -fsSL https://opencode.ai/v2/install | bash` from MSYS2, or the
npm tarball as above).

On Windows the files live in other places:

- Config: `%APPDATA%\vikunja-popups\config.json`
- State (proposals, last email) and log file `app.log`:
  `%LOCALAPPDATA%\vikunja-popups\`

"Stop app" in the settings ends the running app until it is started again or
the next login. Remove the app with
`powershell -ExecutionPolicy Bypass -File uninstall.ps1` (keeps the config and
MSYS2).

Windows support is new and less tested than Linux: if the popups look or
behave differently (transparency, staying on top, focus of the input boxes),
please check `app.log`.

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
projects and to read tasks in them; to create tasks from the tabs, it also
needs permission to create tasks. For the AI assistant it additionally needs
to create projects (for **ToDo**), update tasks and add comments.

## Setting up the AI assistant

1. Install [opencode](https://opencode.ai) and log in to a model provider
   (`opencode auth login`). The app finds `opencode` on your `PATH` or in
   `~/.opencode/bin/`.
2. In **Vikunja Popups Settings**, choose the **AI model** (the list comes
   from `opencode models`; you can also type a `provider/model` id).
3. For emails: fill in the **Email** page (IMAP for reading, SMTP for sending
   replies) and use "Test login".
4. For GitHub: on the **GitHub** page, "Create token" opens GitHub's page for
   a fine-grained token with read access to issues and pull requests; paste
   it and use "Test login".
5. Save. The assistant starts with what arrives from now on; old emails and
   GitHub items are never processed.

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
  that `opencode models` lists. Used by the email and GitHub assistants (below).
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

## Inbox and proposals

Both assistants put their proposals into the purple **Inbox · N** tab at the
top of the tab column. It is hidden while there is nothing to review.

- Each card shows the email or GitHub item (the GitHub title links to GitHub),
  a short summary by the AI and the tasks it belongs to.
- Each proposed action has a checkbox. Task titles, descriptions, comments
  and the reply can be edited. Nothing happens until you click
  "Execute selected"; unticked actions are dropped, `×` discards the whole
  card.
- Actions that fail stay on the card with the error, so you can try again.
- **Ask again…** opens a box in the middle of the screen for additional
  instructions, such as "Please write in English" or "It should be a new
  task". "Send to AI" (or Ctrl+Enter) sends the item together with the
  previous proposal and your instructions back to the AI; its new answer
  replaces the card. If that fails, the old proposal and your edits stay.
- **New tasks always go to the ToDo project.** If you don't have one, it is
  created the first time such a task is executed.
- Open proposals are kept in `~/.local/state/vikunja-popups/state.json`
  (mode `0600`), so they survive restarts.

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
- Emails that need something get a card in the [Inbox](#inbox-and-proposals);
  newsletters, notifications and spam are skipped.
- Replies go to the sender (or `Reply-To`) from your `address` via SMTP, as an
  answer in the same thread with the original quoted. They are not copied to a
  Sent folder.
- At most 10 emails are handled per refresh. If the AI cannot be reached, the
  same email is tried again on the next refresh.
- The AI runs through `opencode run --standalone` with its own agent
  (`vikunja-popups-mail`) that may not use any tools, so text in an email
  cannot make it run commands or change files. Each email creates an opencode
  session. Email text and your task list are sent to the model's provider.
- The last handled email is kept in
  `~/.local/state/vikunja-popups/state.json`. Delete the file to start over
  from "now" (this also drops open proposals).

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
  comment or description.
- Proposals appear in the [Inbox](#inbox-and-proposals) like the email ones.
  GitHub items get no reply action. "Ask again…" is not available for GitHub
  cards stored by an older version of the app.
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

On Windows: `powershell -ExecutionPolicy Bypass -File uninstall.ps1`.
