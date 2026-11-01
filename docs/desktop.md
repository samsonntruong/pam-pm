# Pam desktop wrapper (beta)

`pam desktop` runs Pam in its own window with a tray icon instead of a
browser tab. It starts the local server when it is not already running
(health check on 127.0.0.1:8765, never two instances), then opens the
console in a window titled Pam.

Only one desktop instance runs at a time. A second `pam desktop` prints
a note and exits, leaving the first one alone.

## Install

Windows and macOS install the desktop extra (`pywebview`, `pystray`,
`Pillow`) by default:

```powershell
powershell -ExecutionPolicy ByPass -File install.ps1
```

```sh
sh install.sh
```

To skip it and stay browser only:

```powershell
powershell -ExecutionPolicy ByPass -File install.ps1 -NoDesktop
```

```sh
sh install.sh --no-desktop
```

Linux stays browser only. The installer never adds the desktop extra
there, and `--no-desktop` is accepted but changes nothing.

Manual install with pip:

```sh
pip install "pam-pm[desktop]"
pam desktop
```

## Tray menu

The tray icon offers four entries:

- Open Pam: shows the window again after you closed (hid) it.
- Pause check-ins / Resume check-ins: pauses or resumes the morning
  scheduler rule (see below).
- Start at sign-in: checked when autostart is on. Flipping it runs the
  same step as `pam autostart on` or `pam autostart off`.
- Quit: closes the window and tray, and stops Pam.

## Tray status

The icon color and tooltip report one of three states:

- running (green): the console answers and check-ins are active.
- listening (amber): the console answers but check-ins are paused.
  Pam still listens, it just sends no morning check-ins.
- offline (gray): the console does not answer.

## Pause check-ins

Pause check-ins turns off the morning check-in rule on the scheduler,
so Pam sends no morning DMs until you resume. The paused state is saved
in your data directory, so it survives restarts. Resume check-ins turns
the rule back on.

## Start at sign-in

When the desktop extra is installed, `pam autostart on` launches
`pam desktop` at sign-in, so the window and tray come back after a
reboot. Without the desktop extra it launches `pam start --no-browser`
instead. Either way Pam restarts when it stops (Scheduled Task on
Windows, LaunchAgent with KeepAlive on macOS, systemd user service with
Restart=on-failure on Linux).

## Closing the window vs Quit

Closing the window only hides it. Pam keeps running in the background,
and Open Pam in the tray menu brings the window back. Only Quit stops
Pam: it closes the window and tray, and stops the server when this
desktop session started it. When Pam was already running before you ran
`pam desktop` (you ran `pam start` first), Quit leaves that server
running and only closes the window and tray.

## When the window cannot open

When pywebview is not installed, `pam desktop` prints a plain message
and opens the console in your default browser instead. Everything still
works, you just get a browser tab rather than the Pam window. Install
the desktop extra (above) to get the window.

When pystray or Pillow is missing, the window still works but there is
no tray icon. Closing the window then still hides it; run `pam desktop`
again or open the console URL to get back in.

## Windows SmartScreen note

Pam is not code signed, so the first launch may show a Windows
SmartScreen prompt ("Windows protected your PC"). This is expected for
unsigned apps. Click "More info", then "Run anyway" to continue. No
admin rights are needed at any step.

## Troubleshooting

- "Pam desktop is already running": the tray icon is already there.
  Use Open Pam in the tray menu. When no icon is visible, stop any
  stray `pam desktop` process and try again.
- "Pam did not start in time": run `pam doctor` to check the setup,
  then try `pam desktop` again. Server output is logged to
  `desktop-server.log` in the Pam data directory.
- Stuck lock file: the single-instance lock lives at `desktop.lock` in
  the Pam data directory. It clears itself when the owning process is
  gone. Delete it only when no Pam process is running.
