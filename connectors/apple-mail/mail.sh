#!/bin/bash
# Письма из Почты macOS: mail.sh <часов_назад> [символов=500] [inbox|sent] [фильтр]
DIR="$(cd "$(dirname "$0")" && pwd)"
pgrep -x Mail >/dev/null || { open -g -a Mail; /bin/sleep 20; }
exec osascript -l JavaScript "$DIR/mail.js" "$@"
