#!/bin/bash
# Черновики писем в Почте macOS, без отправки: draft.sh <файл.json>
DIR="$(cd "$(dirname "$0")" && pwd)"
IN="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
exec osascript -l JavaScript "$DIR/mail_draft.js" "$IN"
