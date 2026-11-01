# Installing Pam

One command, no admin rights, no Python or git needed. The installer uses
[uv](https://docs.astral.sh/uv/) (Astral's installer), which lives in your user
profile and downloads Python 3.12 itself.

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/samsonntruong/pam-pm/main/install.ps1 | iex"
```

macOS or Linux:

```sh
curl -LsSf https://raw.githubusercontent.com/samsonntruong/pam-pm/main/install.sh | sh
```

Your browser opens the Pam setup console. Follow the wizard.

Prefer to read the script first? Download `install.ps1` (Windows) or
`install.sh` (macOS or Linux) from the repo root, read it, then run it:

```powershell
powershell -ExecutionPolicy ByPass -File install.ps1
```

```sh
sh install.sh
```

The installer never asks for or handles secrets. You enter all credentials in
the web wizard. Re-running the installer upgrades Pam in place and never
touches the data directory.

Installer options (PowerShell `-Name` / shell `--name`): `-Version` (release
tag, default is the current release constant in the script, update it when
tagging), `-Ref` (branch, e.g. `main`), `-Source` (local path or URL, for
testing), `-NoStart`, `-NoAutostart`, `-NoDesktop`, `-Uninstall` (keeps data),
`-RemoveData` (with `-Uninstall` only, asks for confirmation), `-DryRun`
(print commands only).

Desktop note: on Windows and macOS the installer also sets up the desktop
wrapper (beta) unless `-NoDesktop` / `--no-desktop` is passed. After install,
`pam desktop` opens Pam in its own window with a tray icon instead of a
browser tab. See [desktop.md](desktop.md).


## Per-OS notes

Windows: run the PowerShell one-liner with `install.ps1`; it installs with `uv tool install pam-pm[desktop]` so `pam desktop` opens the Pam window with a tray icon. The installer adds a Start Menu shortcut named Pam (in the per-user Programs folder) that runs `pam desktop` with the Pam logo; double-clicking it starts the server when needed and opens the console in the Pam window, with server output and the first-run setup token in desktop-server.log in the data directory. Pass `-NoDesktop` to skip the shortcut. Autostart uses a per-user Scheduled Task at sign-in with restart on failure, no admin needed. Known limits: file mode bits are not POSIX, so secret files rely on the user profile ACL instead of 0600; SmartScreen may prompt on the downloaded script.

macOS: run the shell one-liner with `install.sh`; it installs `pam-pm[desktop]` on Darwin so `pam desktop` works. The installer adds Pam.app to ~/Applications (or /Applications when that is the writable apps folder, never with sudo); double-clicking it runs `pam desktop`, which starts the server when needed and opens the console in the Pam window, logging launcher output to desktop-app.log with the first-run setup token in desktop-server.log. Pass `--no-desktop` to skip Pam.app. Autostart uses a LaunchAgent with KeepAlive (bootstrap with load fallback). Known limits: install CI runs on workflow_dispatch only to save minutes; first launch may need a right-click Open approval for the unsigned window backend.

Linux: run the shell one-liner with `install.sh`; install is browser-only because desktop tray support varies by Wayland and X11 setup. The installer still writes Pam.desktop to ~/.local/share/applications with the Pam logo as its icon (best effort desktop extra); double-clicking it runs `pam desktop`, which starts the server when needed and opens the console in a browser tab when the desktop backends are missing, logging launcher output to desktop-app.log with the first-run setup token in desktop-server.log. Pass `--no-desktop` to skip the desktop file. Autostart uses a systemd user service with Restart=on-failure. Known limits: `pam desktop` falls back to the browser when pywebview or pystray backends are missing; headless servers should use `pam start --no-browser` instead.

## Upgrade

Re-run the same one-liner. It reinstalls Pam in place over the old version.
Your data and settings are kept.

## Uninstall

Windows:

```powershell
# Download the script first, then:
powershell -ExecutionPolicy ByPass -File install.ps1 -Uninstall
```

macOS or Linux:

```sh
sh install.sh --uninstall
```

This runs `uv tool uninstall pam-pm` and keeps your data. To also delete all
Pam data, add `-RemoveData` / `--remove-data`; the script asks you to type
YES before deleting.

## Where data lives

Pam stores its database and settings in the OS user data directory for the
app name `pam-pm`, unless `PAM_HOME` is set (it wins when set):

- Windows: `%LOCALAPPDATA%\pam-pm`
- macOS: `~/Library/Application Support/pam-pm`
- Linux: `~/.local/share/pam-pm`

Back up or move that folder to back up or move Pam.

## Keeping Pam running

Pam only works while the machine is on and awake. Sleep, hibernate, reboot,
or shutdown pauses Pam, and closing the lid pauses it too. When the machine
wakes again, Pam comes back by itself and picks up where it left off,
without sending any message or board write twice.

Autostart is on by default. Pam starts at sign-in and restarts if it stops:
a per-user Scheduled Task on Windows, a LaunchAgent with KeepAlive on
macOS, and a systemd user service with Restart=on-failure on Linux. The
setup wizard Done screen has the same checkbox on by default, and the
installers accept `-NoAutostart` / `--no-autostart` to skip it. Manage it
any time:

```sh
pam autostart status
pam autostart off
pam autostart on
```

`pam doctor` shows autostart status, last start, and uptime. To keep Pam
running overnight, turn off sleep in the OS power settings (Windows:
Settings, System, Power; macOS: System Settings, Battery, Options; Linux:
Settings, Power) or leave the machine awake with the lid open.

For always-on use on a server instead of a laptop or desktop, see
docs/deploy.md.

## Troubleshooting

SmartScreen or antivirus prompt (Windows): the one-liner runs a script
downloaded from GitHub, so SmartScreen may ask for confirmation. Click
"More info" then "Run anyway" only if you fetched it from this repo. If your
antivirus blocks it, download `install.ps1` first (see above), read it, and
run it from the file with `-ExecutionPolicy ByPass` as shown.

Corporate proxy: `irm`/`curl` and `uv` honor the `HTTPS_PROXY` (and
`https_proxy`) environment variables. Set them in the same terminal before
running the installer:

```powershell
$env:HTTPS_PROXY = "http://proxy:8080"
```

```sh
export HTTPS_PROXY=http://proxy:8080
```

`pam` not found after install: the installer puts `pam` on PATH for new
terminals via `uv tool update-shell`. Open a new terminal window and try
again. If it still fails, run the full path: `%USERPROFILE%\.local\bin\pam`
(Windows) or `~/.local/bin/pam` (macOS or Linux).

Port 8765 already in use: start Pam on another port:

```sh
pam start --port 8877
```

Then open the setup URL it prints.

## uv commands used (verified)

Verified against https://docs.astral.sh/uv/ and `uv --help` output on
2026-11-01 with local uv 0.11.1:

- Installer scripts: `https://astral.sh/uv/install.ps1` (Windows) and
  `https://astral.sh/uv/install.sh` (macOS or Linux). Confirmed in the
  install docs (both redirect to signed builds on releases.astral.sh).
- Install: `uv tool install --python 3.12 --force <source>` with the archive
  URL or path as the positional package argument. Note: the first draft of
  this installer used `uv tool install --from <archive> pam-pm`, but the
  current docs show the source as the positional argument (e.g.
  `uv tool install git+https://github.com/...`) and `uv tool install --help`
  no longer lists `--from` (it only documents `--from` for `uv tool run`).
  A live test confirmed the positional form installs from a zip archive
  over both a local path and an http URL. `--python 3.12` and `--force`
  are confirmed flags of `uv tool install`.
- `uv tool update-shell`: confirmed, ensures the tool bin dir is on PATH.
- `uv tool uninstall pam-pm`: confirmed, takes the tool name positionally.
