#!/usr/bin/env bash

set -euo pipefail

APP_VERSION="$(python3 - <<'PY'
import ast
from pathlib import Path

tree = ast.parse(Path("offline_downloader_gui.py").read_text(encoding="utf-8"))
for node in tree.body:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "APP_VERSION":
                print(ast.literal_eval(node.value))
                raise SystemExit
raise SystemExit("APP_VERSION not found")
PY
)"
VERSION="${VERSION:-$APP_VERSION}"
if [ "$VERSION" != "$APP_VERSION" ]; then
  echo "VERSION ($VERSION) must match APP_VERSION ($APP_VERSION)" >&2
  exit 1
fi
ARCH="amd64"
PACKAGE="offline-downloader"
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$ROOT_DIR/build/deb"
PKG_DIR="$BUILD_DIR/${PACKAGE}_${VERSION}_${ARCH}"
OUT_FILE="$ROOT_DIR/${PACKAGE}_${VERSION}_${ARCH}.deb"

rm -rf "$BUILD_DIR"
mkdir -p "$PKG_DIR/DEBIAN"
mkdir -p "$PKG_DIR/opt/offline-downloader"
mkdir -p "$PKG_DIR/usr/bin"
mkdir -p "$PKG_DIR/usr/share/applications"

install -m 755 "$ROOT_DIR/offline_downloader_gui.py" "$PKG_DIR/opt/offline-downloader/offline_downloader_gui.py"
install -m 755 "$ROOT_DIR/offline_downloader.sh" "$PKG_DIR/opt/offline-downloader/offline_downloader.sh"
install -m 755 "$ROOT_DIR/OfflineDownloader" "$PKG_DIR/opt/offline-downloader/OfflineDownloader"
install -m 644 "$ROOT_DIR/README.md" "$PKG_DIR/opt/offline-downloader/README.md"
install -m 644 "$ROOT_DIR/offline-downloader.desktop" "$PKG_DIR/opt/offline-downloader/offline-downloader.desktop"
install -m 644 "$ROOT_DIR/offline-downloader.desktop" "$PKG_DIR/usr/share/applications/offline-downloader.desktop"

cat > "$PKG_DIR/usr/bin/offline-downloader" <<'LAUNCHER'
#!/usr/bin/env bash
exec /opt/offline-downloader/OfflineDownloader "$@"
LAUNCHER
chmod 755 "$PKG_DIR/usr/bin/offline-downloader"

cat > "$PKG_DIR/DEBIAN/control" <<CONTROL
Package: offline-downloader
Version: $VERSION
Section: net
Priority: optional
Architecture: $ARCH
Depends: python3, python3-gi, gir1.2-gtk-3.0, git
Maintainer: Tim Anderson <TimAnderson1992@users.noreply.github.com>
Description: Download-only offline backup manager
 Offline Downloader is a Linux Mint GTK app for downloading and updating
 offline backup copies of GitHub repositories, direct files, release assets,
 and Linux Mint ISO files.
CONTROL

dpkg-deb --root-owner-group --build "$PKG_DIR" "$OUT_FILE"
echo "$OUT_FILE"
