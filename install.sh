#!/usr/bin/env bash
# install.sh: sets up OrinTube for the current user (no sudo).
#
# Run from the repository:  ./install.sh [--with-deno]
#
# What it does:
#   1. Checks the system pieces it cannot install itself (they need sudo / JetPack):
#      NVIDIA's GStreamer elements (nvv4l2decoder, nv3dsink), the GStreamer plugin sets
#      and PyGObject. It lists the apt command for anything missing and stops.
#   2. pip install --user -U yt-dlp yt-dlp-ejs   (yt-dlp-ejs: YouTube's JS challenge solver)
#   3. Checks for deno (the JavaScript runtime yt-dlp-ejs needs). With --with-deno it
#      downloads deno for this CPU from github.com/denoland/deno releases, checks its
#      published SHA-256, and puts it in ~/.deno/bin. Without the flag it explains how.
#   4. Links bin/orintube into ~/.local/bin (an existing file that is not a
#      link is kept as <name>.bak), installs the icon and adds "OrinTube" to the app grid.
#
# Re-running it is safe: it updates yt-dlp and refreshes the links.

set -euo pipefail

# Assigned before readonly: "readonly X=$(cmd)" would hide a failure of cmd from set -e.
REPO_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
readonly REPO_DIR
readonly BIN_DIR="$HOME/.local/bin"
readonly APPLICATIONS_DIR="$HOME/.local/share/applications"
readonly ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
readonly DENO_DIR="$HOME/.deno/bin"
readonly DENO_VERSION="v2.9.7"

WITH_DENO=false
for argument in "$@"; do
    case "$argument" in
        --with-deno) WITH_DENO=true ;;
        *) echo "usage: ./install.sh [--with-deno]" >&2; exit 2 ;;
    esac
done

check_system_pieces() {
    # Stop with the apt command to run when a GStreamer element or PyGObject is missing.
    local missing_packages=() element
    for element in nvv4l2decoder nv3dsink; do
        if ! gst-inspect-1.0 "$element" >/dev/null 2>&1; then
            echo "Missing NVIDIA GStreamer element '$element'. This app needs a Jetson with JetPack 6." >&2
            exit 1
        fi
    done
    gst-inspect-1.0 hlsdemux >/dev/null 2>&1 || missing_packages+=(gstreamer1.0-plugins-bad)
    gst-inspect-1.0 souphttpsrc >/dev/null 2>&1 || missing_packages+=(gstreamer1.0-plugins-good)
    gst-inspect-1.0 avdec_aac >/dev/null 2>&1 || missing_packages+=(gstreamer1.0-libav)
    gst-inspect-1.0 alsasink >/dev/null 2>&1 || missing_packages+=(gstreamer1.0-alsa)
    python3 -c "import gi; gi.require_version('Gtk', '3.0'); gi.require_version('Gst', '1.0'); gi.require_version('GstVideo', '1.0')" 2>/dev/null \
        || missing_packages+=(python3-gi gir1.2-gtk-3.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0)
    python3 -c "import cairo" 2>/dev/null || missing_packages+=(python3-gi-cairo)
    python3 -m pip --version >/dev/null 2>&1 || missing_packages+=(python3-pip)
    if (( ${#missing_packages[@]} )); then
        echo "Missing system packages. Install them, then run ./install.sh again:" >&2
        echo "  sudo apt install ${missing_packages[*]}" >&2
        exit 1
    fi
    echo "System pieces: OK"
}

install_python_packages() {
    # yt-dlp changes often to keep up with YouTube, so -U also updates an existing install.
    python3 -m pip install --user -U yt-dlp yt-dlp-ejs
}

deno_asset_name() {
    # Print deno's release file name for this CPU (aarch64 on a Jetson).
    case "$(uname -m)" in
        aarch64) echo "deno-aarch64-unknown-linux-gnu.zip" ;;
        x86_64) echo "deno-x86_64-unknown-linux-gnu.zip" ;;
        *) echo "Unsupported CPU for deno: $(uname -m)" >&2; return 1 ;;
    esac
}

install_deno() {
    # Download deno, verify it against the SHA-256 file deno publishes next to it, unpack to DENO_DIR.
    local asset base_url
    asset="$(deno_asset_name)"
    base_url="https://github.com/denoland/deno/releases/download/$DENO_VERSION"
    # Global, not local: the EXIT trap runs after this function is gone when set -e stops
    # the script on a failed download or checksum, and must still find the folder.
    DENO_DOWNLOAD_DIR="$(mktemp -d)"
    trap 'rm -rf "$DENO_DOWNLOAD_DIR"' EXIT
    local download_dir="$DENO_DOWNLOAD_DIR"
    curl -fsSL -o "$download_dir/$asset" "$base_url/$asset"
    curl -fsSL -o "$download_dir/$asset.sha256sum" "$base_url/$asset.sha256sum"
    (cd "$download_dir" && sha256sum -c "$asset.sha256sum")
    mkdir -p "$DENO_DIR"
    python3 -I -c "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extract('deno', sys.argv[2])" \
        "$download_dir/$asset" "$download_dir"
    install -m 755 "$download_dir/deno" "$DENO_DIR/deno"
}

ensure_deno() {
    # yt-dlp finds deno through PATH, so it is linked into ~/.local/bin.
    if [[ -x "$DENO_DIR/deno" ]] || command -v deno >/dev/null 2>&1; then
        echo "deno: found"
    elif $WITH_DENO; then
        install_deno
    else
        echo "deno is not installed. Most videos still play, but some need it (YouTube's JS check)."
        echo "Run ./install.sh --with-deno to download deno $DENO_VERSION from GitHub (about 40 MB)."
        return 0
    fi
    if [[ -x "$DENO_DIR/deno" ]]; then
        ln -sf "$DENO_DIR/deno" "$BIN_DIR/deno"
    fi
}

link_command() {
    # Link bin/<name> into BIN_DIR, keeping an existing regular file as <name>.bak.
    local name="$1" target="$BIN_DIR/$1"
    if [[ -e "$target" && ! -L "$target" ]]; then
        mv "$target" "$target.bak"
        echo "Kept the old $target as $target.bak"
    fi
    ln -sf "$REPO_DIR/bin/$name" "$target"
}

install_launchers() {
    # Commands in ~/.local/bin, the app icon, and the "OrinTube" app-grid entry.
    mkdir -p "$BIN_DIR" "$APPLICATIONS_DIR" "$ICON_DIR"
    install -m 644 "$REPO_DIR/data/orintube.svg" "$ICON_DIR/orintube.svg"
    link_command orintube
    # 2026-10-19 (user: "orin tube lost the icon ... its just a gear"): with Icon=orintube the
    # dock kept showing a gear although GTK found the icon (GNOME Shell had not re-read its
    # icon cache). A full path to the file always works.
    sed -e "s|@BIN_DIR@|$BIN_DIR|" -e "s|@ICON_PATH@|$ICON_DIR/orintube.svg|" \
        "$REPO_DIR/data/orintube.desktop.in" > "$APPLICATIONS_DIR/orintube.desktop"
    echo "Installed: the orintube command and OrinTube in the app grid."
}

mkdir -p "$BIN_DIR"
check_system_pieces
install_python_packages
ensure_deno
install_launchers
echo "Done. Start it with: orintube   (sign in to YouTube in Chromium or Chrome for your feeds)"
