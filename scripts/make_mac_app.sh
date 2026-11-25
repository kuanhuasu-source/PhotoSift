#!/bin/bash
# Builds PhotoSift.app: a small launcher that opens the desktop window.
# The app runs this checkout in place, so keep the folder where it is after building.
#
#   ./scripts/make_mac_app.sh            -> ./PhotoSift.app
#   then drag PhotoSift.app to /Applications or the Dock
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/PhotoSift.app"
UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
  echo "找不到 uv。請先安裝：https://docs.astral.sh/uv/" >&2
  exit 1
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>PhotoSift</string>
  <key>CFBundleDisplayName</key><string>PhotoSift</string>
  <key>CFBundleIdentifier</key><string>local.photosift.app</string>
  <key>CFBundleVersion</key><string>0.1.0</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleExecutable</key><string>PhotoSift</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# Apps started from Finder get a bare PATH, so the launcher uses absolute paths.
cat > "$APP/Contents/MacOS/PhotoSift" <<LAUNCH
#!/bin/bash
cd "$ROOT"
exec "$UV" run --no-dev photosift >> "\$HOME/Library/Logs/PhotoSift.log" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/PhotoSift"

echo "已建立 $APP"
echo "第一次開啟時，如果 macOS 說無法驗證開發者：在 Finder 對它按右鍵，選「打開」。"
