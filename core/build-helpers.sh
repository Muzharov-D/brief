#!/bin/bash
# Сборка помощников Календаря и Напоминаний (EventKit) для Mac: arm64 + x86_64, macOS 14+.
# Запуск: core/build-helpers.sh <папка_вывода>   (по умолчанию helpers/ в корне)
# Подпись делает установщик на целевом Mac (в папке iCloud codesign не работает).
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; OUT="${1:-$ROOT/helpers}"; mkdir -p "$OUT"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# SDK, который подходит к установленному компилятору (самый новый SDK бывает новее swiftc)
echo 'print(1)' > "$TMP/t.swift"; SDK=""
for c in "${BRIEF_SDK:-}" "$(xcrun --show-sdk-path 2>/dev/null)" $(ls -dr /Library/Developer/CommandLineTools/SDKs/MacOSX[0-9]*.sdk 2>/dev/null); do
  [ -n "$c" ] && swiftc -sdk "$c" -target arm64-apple-macos14.0 "$TMP/t.swift" -o "$TMP/t" 2>/dev/null && { SDK="$c"; break; }
done
[ -n "$SDK" ] || { echo "нет подходящего SDK для swiftc"; exit 1; }
build() { # имя_приложения исполняемый bundle_id swift описание_доступа ключи_plist
  local app="$1" exe="$2" bid="$3" src="$4" extra="$5"
  for a in arm64 x86_64; do swiftc -O -sdk "$SDK" -target "$a-apple-macos14.0" "$src" -o "$TMP/$exe-$a"; done
  local dst="$TMP/$app.app/Contents"; mkdir -p "$dst/MacOS"
  lipo -create "$TMP/$exe-arm64" "$TMP/$exe-x86_64" -output "$dst/MacOS/$exe"
  cat > "$dst/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleDisplayName</key><string>$app</string><key>CFBundleName</key><string>$app</string>
<key>CFBundleExecutable</key><string>$exe</string><key>CFBundleIdentifier</key><string>$bid</string>
<key>CFBundlePackageType</key><string>APPL</string><key>CFBundleShortVersionString</key><string>1.0</string>
<key>CFBundleVersion</key><string>1</string><key>LSMinimumSystemVersion</key><string>14.0</string>
<key>LSUIElement</key><true/>
$extra
</dict></plist>
PL
  rm -rf "$OUT/$app.app"; ditto --noextattr "$TMP/$app.app" "$OUT/$app.app"
}
build "Brief Calendar" brief-calendar local.brief.calendar "$ROOT/connectors/apple-calendar/calendar.swift" \
  '<key>NSCalendarsFullAccessUsageDescription</key><string>Огонёк читает встречи из Календаря: утренний брифинг и справки перед встречами. Менять и удалять встречи не может.</string>
<key>NSCalendarsUsageDescription</key><string>Огонёк читает встречи из Календаря.</string>
<key>NSContactsUsageDescription</key><string>Огонёк читает дни рождения из Контактов.</string>'
build "Brief Reminders" brief-reminders local.brief.reminders "$ROOT/connectors/apple-reminders/reminders.swift" \
  '<key>NSRemindersFullAccessUsageDescription</key><string>Огонёк ведёт задачи в своих списках Напоминаний и читает сроки. Удалять задачи не может.</string>
<key>NSRemindersUsageDescription</key><string>Огонёк ведёт задачи в своих списках Напоминаний.</string>'
# Огонёк.app (исполняемый файл латиницей: с «ё» в имени codesign не подписывает): одна запись в «Объектах входа» и понятное имя в запросах разрешений
build "Огонёк" ogonyok local.brief.app "$ROOT/core/app/launcher.swift" \
  '<key>CFBundleIconFile</key><string>Огонёк</string>
<key>NSAppleEventsUsageDescription</key><string>Огонёк читает письма из приложения Почта и готовит черновики. Отправляете письма вы сами.</string>'
ICS="$TMP/Огонёк.iconset"; mkdir -p "$ICS" "$OUT/Огонёк.app/Contents/Resources"
for s in 16 32 128 256 512; do
  sips -z $s $s "$ROOT/design/маскот/огонёк/огонёк-круг-640.png" --out "$ICS/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$ROOT/design/маскот/огонёк/огонёк-круг-640.png" --out "$ICS/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICS" -o "$OUT/Огонёк.app/Contents/Resources/Огонёк.icns"
echo "готово: $OUT"
