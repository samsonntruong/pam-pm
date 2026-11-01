#!/bin/sh
# Pam installer for macOS and Linux (POSIX sh).
# No admin rights, no Docker. Re-running upgrades Pam in place, data untouched.
# This script never asks for or handles secrets. Enter credentials in the web wizard.
#
# One-liner:
#   curl -LsSf https://raw.githubusercontent.com/OWNER/pam-pm/main/install.sh | sh
#
# Options:
#   --version TAG   Release tag to install (default: 3a default below, update when tagging)
#   --ref BRANCH    Install from a branch instead, e.g. --ref main
#   --source P      Local path or URL to install from (for testing)
#   --no-start      Install but do not start Pam
#   --no-autostart  Do not enable start on login
#   --no-desktop    Skip the desktop window and tray icon (browser only; Linux is always browser only)
#   --uninstall     Remove Pam, keeping the data directory
#   --remove-data   With --uninstall only: also delete the data directory (asks first)
#   --dry-run       Print the commands without running them

set -u

# Default release tag. Update this constant when tagging a release.
# Mirrors the version in pyproject.toml and src/pam_pm/__init__.py (__version__).
DEFAULT_VERSION="v0.2.0"
UV_INSTALL_SCRIPT="https://astral.sh/uv/install.sh"
REPO_ARCHIVE_BASE="https://github.com/OWNER/pam-pm/archive/refs"

VERSION=""
REF=""
SOURCE=""
NO_START=0
NO_AUTOSTART=0
NO_DESKTOP=0
UNINSTALL=0
REMOVE_DATA=0
DRY_RUN=0

usage() {
    echo "Usage: install.sh [--version TAG] [--ref BRANCH] [--source PATH_OR_URL]"
    echo "                  [--no-start] [--no-autostart] [--no-desktop]"
    echo "                  [--uninstall] [--remove-data] [--dry-run]"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --ref) REF="$2"; shift 2 ;;
        --source) SOURCE="$2"; shift 2 ;;
        --no-start) NO_START=1; shift ;;
        --no-autostart) NO_AUTOSTART=1; shift ;;
        --no-desktop) NO_DESKTOP=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        --remove-data) REMOVE_DATA=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

step() { printf '\n==> %s\n' "$1"; }
fail() { printf '\nFAILED: %s\n' "$1"; exit 1; }

uv_bin_dir() { printf '%s/.local/bin' "$HOME"; }

data_dir() {
    if [ -n "${PAM_HOME:-}" ]; then
        printf '%s' "$PAM_HOME"
    elif [ "$(uname -s)" = "Darwin" ]; then
        printf '%s/Library/Application Support/pam-pm' "$HOME"
    else
        printf '%s/.local/share/pam-pm' "$HOME"
    fi
}

run() {
    label="$1"; dry="$2"; shift 2
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "  [dry run] $dry"
        return 0
    fi
    echo "  $label"
    "$@"
}

if [ "$REMOVE_DATA" -eq 1 ] && [ "$UNINSTALL" -eq 0 ]; then
    fail "--remove-data only works together with --uninstall."
fi

if [ "$UNINSTALL" -eq 1 ]; then
    step "Removing Pam (your data stays in place)."
    run "Running: uv tool uninstall pam-pm" "uv tool uninstall pam-pm" \
        uv tool uninstall pam-pm
    if [ "$REMOVE_DATA" -eq 1 ]; then
        dir="$(data_dir)"
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "  [dry run] Ask for confirmation, then remove $dir"
        else
            printf 'Delete all Pam data in %s ? Type YES to confirm: ' "$dir"
            read -r answer
            if [ "$answer" = "YES" ]; then
                if [ -d "$dir" ]; then
                    rm -rf "$dir"
                    echo "  Data directory removed."
                else
                    echo "  Data directory not found, nothing to remove."
                fi
            else
                echo "  Keeping the data directory."
            fi
        fi
    else
        echo "  Data kept in $(data_dir)."
    fi
    printf '\nPam is uninstalled.\n'
    exit 0
fi

if [ -z "$VERSION" ]; then VERSION="$DEFAULT_VERSION"; fi

INSTALL_SOURCE="$SOURCE"
if [ -z "$INSTALL_SOURCE" ] && [ -n "$REF" ]; then
    INSTALL_SOURCE="$REPO_ARCHIVE_BASE/heads/$REF.zip"
fi
if [ -z "$INSTALL_SOURCE" ]; then
    INSTALL_SOURCE="$REPO_ARCHIVE_BASE/tags/$VERSION.zip"
fi

step "Step 1 of 4: Checking for uv (the Python tool installer)."
if ! command -v uv >/dev/null 2>&1; then
    run "uv not found, installing it into your profile." \
        "curl -LsSf $UV_INSTALL_SCRIPT | sh" \
        sh -c "curl -LsSf $UV_INSTALL_SCRIPT | sh"
    if [ "$DRY_RUN" -eq 0 ] && ! command -v uv >/dev/null 2>&1; then
        if [ -x "$(uv_bin_dir)/uv" ]; then
            PATH="$(uv_bin_dir):$PATH"
            export PATH
        fi
    fi
    if [ "$DRY_RUN" -eq 0 ] && ! command -v uv >/dev/null 2>&1; then
        fail "uv installed but was not found. Open a new terminal and run this script again."
    fi
else
    echo "  uv is already installed."
fi

step "Step 2 of 4: Making uv available in this shell."
if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry run] Add $(uv_bin_dir) to session PATH"
else
    case ":$PATH:" in
        *":$(uv_bin_dir):"*) : ;;
        *) PATH="$(uv_bin_dir):$PATH"; export PATH ;;
    esac
    echo "  Done."
fi

DESKTOP_WANTED=0
if [ "$NO_DESKTOP" -eq 0 ] && [ "$(uname -s)" = "Darwin" ]; then
    DESKTOP_WANTED=1
fi
INSTALL_SPEC="$INSTALL_SOURCE"
if [ "$DESKTOP_WANTED" -eq 1 ]; then
    INSTALL_SPEC="pam-pm[desktop] @ $INSTALL_SOURCE"
fi

step "Step 3 of 4: Installing Pam from $INSTALL_SOURCE."
echo "  Re-running later upgrades Pam in place. Your data is never touched."
run "Running: uv tool install --python 3.12 --force $INSTALL_SPEC" \
    "uv tool install --python 3.12 --force $INSTALL_SPEC" \
    uv tool install --python 3.12 --force "$INSTALL_SPEC"

run "Running: uv tool update-shell" "uv tool update-shell" \
    uv tool update-shell

if [ "$NO_AUTOSTART" -eq 0 ]; then
    PAM_AUTOSTART_EXE="$(uv_bin_dir)/pam"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "  [dry run] $PAM_AUTOSTART_EXE autostart on"
    elif [ -x "$PAM_AUTOSTART_EXE" ]; then
        echo "  Running: $PAM_AUTOSTART_EXE autostart on"
        "$PAM_AUTOSTART_EXE" autostart on
    else
        echo "  pam not found yet, skipping autostart. Run 'pam autostart on' later."
    fi
fi
if [ "$DESKTOP_WANTED" -eq 1 ]; then
    echo "  Desktop window and tray icon installed. The console opens in the Pam window."
else
    echo "  Browser-only install. The console opens in your browser."
fi

START_COMMAND="start"
if [ "$DESKTOP_WANTED" -eq 1 ]; then
    START_COMMAND="desktop"
fi

if [ "$NO_START" -eq 1 ]; then
    printf '\nPam is installed. Start it any time with: pam %s\n' "$START_COMMAND"
    exit 0
fi

if [ "$DESKTOP_WANTED" -eq 1 ]; then
    step "Step 4 of 4: Starting Pam and opening the setup console in the Pam window."
else
    step "Step 4 of 4: Starting Pam and opening the setup console in your browser."
fi
PAM_EXE="$(uv_bin_dir)/pam"
if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry run] $PAM_EXE $START_COMMAND"
else
    if [ ! -x "$PAM_EXE" ]; then
        fail "Pam installed but $PAM_EXE was not found. Open a new terminal and run: pam $START_COMMAND"
    fi
    "$PAM_EXE" "$START_COMMAND"
fi

if [ "$DESKTOP_WANTED" -eq 1 ]; then
    printf '\nThe Pam window opens the setup console. Follow the wizard.\n'
else
    printf '\nYour browser opens the Pam setup console. Follow the wizard.\n'
fi
