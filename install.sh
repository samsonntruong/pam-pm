#!/bin/sh
# Pam installer for macOS and Linux (POSIX sh).
# No admin rights, no Docker. Re-running upgrades Pam in place, data untouched.
# This script never asks for or handles secrets. Enter credentials in the web wizard.
#
# One-liner:
#   curl -LsSf https://raw.githubusercontent.com/samsonntruong/pam-pm/main/install.sh | sh
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

# HOME is required below; fall back to the passwd entry when unset.
HOME="${HOME:-$(eval echo ~)}"

# Default release tag. Update this constant when tagging a release.
# Mirrors the version in pyproject.toml and src/pam_pm/__init__.py (__version__).
DEFAULT_VERSION="v1.0.0"
UV_INSTALL_SCRIPT="https://astral.sh/uv/install.sh"
REPO_ARCHIVE_BASE="https://github.com/samsonntruong/pam-pm/archive/refs"

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
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "  [dry run] Remove Pam.app, Pam.desktop, and launcher icons"
    else
        rm -rf "$HOME/Applications/Pam.app" 2>/dev/null || true
        if [ -w "/Applications" ]; then
            rm -rf "/Applications/Pam.app" 2>/dev/null || true
        fi
        rm -f "$HOME/.local/share/applications/Pam.desktop" 2>/dev/null || true
        rm -f "$HOME/.local/share/pam-pm/pam-logo.svg" 2>/dev/null || true
    fi
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
INSTALL_SPEC="pam-pm @ $INSTALL_SOURCE"
if [ "$DESKTOP_WANTED" -eq 1 ]; then
    INSTALL_SPEC="pam-pm[desktop] @ $INSTALL_SOURCE"
fi

step "Step 3 of 4: Installing Pam from $INSTALL_SOURCE."
echo "  Re-running later upgrades Pam in place. Your data is never touched."
run "Running: uv tool install --python 3.12 --force \"$INSTALL_SPEC\"" \
    "uv tool install --python 3.12 --force \"$INSTALL_SPEC\"" \
    uv tool install --python 3.12 --force "$INSTALL_SPEC"

run "Running: uv tool update-shell" "uv tool update-shell" \
    uv tool update-shell

SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo "")"

pam_launcher_icon_src() {
    if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/src/pam_pm/web/static/$1" ]; then
        printf '%s' "$SCRIPT_DIR/src/pam_pm/web/static/$1"
        return 0
    fi
    return 1
}

pam_fetch_icon() {
    name="$1"; dest="$2"
    if src="$(pam_launcher_icon_src "$name")" && [ -n "$src" ]; then
        cp "$src" "$dest" 2>/dev/null && return 0
    fi
    if [ -n "$INSTALL_SOURCE" ] && [ -f "$INSTALL_SOURCE" ]; then
        case "$INSTALL_SOURCE" in
            *.zip)
                if command -v unzip >/dev/null 2>&1; then
                    entry="$(unzip -Z1 "$INSTALL_SOURCE" 2>/dev/null | grep "src/pam_pm/web/static/$name" | head -n 1)"
                    if [ -n "$entry" ]; then
                        unzip -p "$INSTALL_SOURCE" "$entry" >"$dest" 2>/dev/null && return 0
                    fi
                elif command -v python3 >/dev/null 2>&1; then
                    python3 - "$INSTALL_SOURCE" "$name" "$dest" 2>/dev/null <<'PYEOF' && return 0
import sys
import zipfile
with zipfile.ZipFile(sys.argv[1]) as zf:
    for n in zf.namelist():
        if n.endswith("src/pam_pm/web/static/" + sys.argv[2]):
            with open(sys.argv[3], "wb") as handle:
                handle.write(zf.read(n))
            break
    else:
        sys.exit(1)
PYEOF
                fi
                ;;
        esac
    fi
    raw_ref="$VERSION"
    if [ -n "$REF" ]; then raw_ref="$REF"; fi
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf "https://raw.githubusercontent.com/samsonntruong/pam-pm/$raw_ref/src/pam_pm/web/static/$name" -o "$dest" 2>/dev/null && return 0
    fi
    return 1
}

install_mac_app() {
    dest_parent="$HOME/Applications"
    if ! mkdir -p "$dest_parent" 2>/dev/null; then
        if [ -w "/Applications" ]; then
            dest_parent="/Applications"
        else
            echo "  Could not write $HOME/Applications, skipping Pam.app."
            return 0
        fi
    fi
    app="$dest_parent/Pam.app"
    rm -rf "$app"
    mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
    pam_exe="$HOME/.local/bin/pam"
    {
        echo "#!/bin/sh"
        echo "# Pam launcher: runs pam desktop, which starts the server"
        echo "# when needed and opens the console window. Output goes to"
        echo "# desktop-app.log; the first-run setup token is in"
        echo "# desktop-server.log in the same folder."
        echo "PAM_EXE=\"$pam_exe\""
        echo 'if [ ! -x "$PAM_EXE" ]; then'
        echo '    PAM_EXE="pam"'
        echo "fi"
        echo 'LOG_DIR="${PAM_HOME:-$HOME/Library/Application Support/pam-pm}"'
        echo 'mkdir -p "$LOG_DIR" 2>/dev/null || true'
        echo 'exec "$PAM_EXE" desktop >>"$LOG_DIR/desktop-app.log" 2>&1'
    } >"$app/Contents/MacOS/Pam"
    chmod +x "$app/Contents/MacOS/Pam"
    icon_name=""
    if pam_fetch_icon "logo.png" "$app/Contents/Resources/pam-logo.png"; then
        icon_name="pam-logo"
        if command -v iconutil >/dev/null 2>&1 && command -v sips >/dev/null 2>&1; then
            tmpset="$(mktemp -d 2>/dev/null || echo "")"
            if [ -n "$tmpset" ] && mkdir -p "$tmpset/pam.iconset"; then
                ok=1
                for sz in 16 32 128 256 512; do
                    sips -z "$sz" "$sz" "$app/Contents/Resources/pam-logo.png" --out "$tmpset/pam.iconset/icon_${sz}x${sz}.png" >/dev/null 2>&1 || ok=0
                done
                for sz in 32 64 256 512 1024; do
                    base=$((sz / 2))
                    sips -z "$sz" "$sz" "$app/Contents/Resources/pam-logo.png" --out "$tmpset/pam.iconset/icon_${base}x${base}@2x.png" >/dev/null 2>&1 || ok=0
                done
                if [ "$ok" -eq 1 ]; then
                    iconutil -c icns "$tmpset/pam.iconset" --output "$app/Contents/Resources/pam-logo.icns" >/dev/null 2>&1 || true
                fi
                rm -rf "$tmpset"
            fi
        fi
    fi
    {
        echo '<?xml version="1.0" encoding="UTF-8"?>'
        echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
        echo '<plist version="1.0">'
        echo "<dict>"
        echo '  <key>CFBundleName</key><string>Pam</string>'
        echo '  <key>CFBundleIdentifier</key><string>com.pam-pm.pam</string>'
        echo "  <key>CFBundleVersion</key><string>$VERSION</string>"
        echo '  <key>CFBundlePackageType</key><string>APPL</string>'
        echo '  <key>CFBundleExecutable</key><string>Pam</string>'
        if [ -n "$icon_name" ]; then
            echo "  <key>CFBundleIconFile</key><string>$icon_name</string>"
        fi
        echo '  <key>NSHighResolutionCapable</key><true/>'
        echo "</dict>"
        echo "</plist>"
    } >"$app/Contents/Info.plist"
    touch "$app"
    echo "  Pam.app installed: $app"
}

install_linux_desktop_file() {
    apps_dir="$HOME/.local/share/applications"
    icons_dir="$HOME/.local/share/pam-pm"
    mkdir -p "$apps_dir" "$icons_dir" 2>/dev/null || true
    pam_exe="$HOME/.local/bin/pam"
    icon_line=""
    if pam_fetch_icon "logo-light.svg" "$icons_dir/pam-logo.svg"; then
        icon_line="Icon=$icons_dir/pam-logo.svg"
    fi
    case "$pam_exe" in
        *\ * ) pam_exec="\"$pam_exe\" desktop" ;;
        * ) pam_exec="$pam_exe desktop" ;;
    esac
    {
        echo "[Desktop Entry]"
        echo "Type=Application"
        echo "Name=Pam"
        echo "Comment=Pam PM console"
        echo "Exec=$pam_exec"
        if [ -n "$icon_line" ]; then echo "$icon_line"; fi
        echo "Terminal=false"
        echo "Categories=Utility;"
        echo "StartupNotify=true"
    } >"$apps_dir/Pam.desktop"
    echo "  Desktop file installed: $apps_dir/Pam.desktop"
}

if [ "$(uname -s)" = "Darwin" ] && [ "$DESKTOP_WANTED" -eq 1 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "  [dry run] Create Pam.app in $HOME/Applications"
    else
        install_mac_app
    fi
fi
if [ "$(uname -s)" != "Darwin" ] && [ "$NO_DESKTOP" -eq 0 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "  [dry run] Create Pam.desktop in ~/.local/share/applications"
    else
        install_linux_desktop_file
    fi
fi

if [ "$NO_AUTOSTART" -eq 0 ]; then
    PAM_AUTOSTART_EXE="$(uv_bin_dir)/pam"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "  [dry run] $PAM_AUTOSTART_EXE autostart on"
    elif [ -x "$PAM_AUTOSTART_EXE" ]; then
        echo "  Running: $PAM_AUTOSTART_EXE autostart on"
        if ! "$PAM_AUTOSTART_EXE" autostart on; then
            echo "  WARNING: autostart registration failed, continuing install. Run 'pam autostart on' later."
        fi
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
