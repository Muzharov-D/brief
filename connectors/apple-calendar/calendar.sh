#!/bin/bash
# Встречи из Календаря macOS: calendar.sh <часов_вперёд> [часов_назад] [birthdays]
DIR="$(cd "$(dirname "$0")" && pwd)"; APP="${BRIEF_HOME:-$HOME/.brief}/helpers/Brief Calendar.app"
[ -d "$APP" ] || { echo '{"error": "Нет помощника Календаря: запустите установку ещё раз (Установить.command)"}'; exit 1; }
OUT="$(mktemp -u -t brief-cal).json"
open -W -g -a "$APP" --args "${1:-40}" "${2:-0}" "$OUT" ${3:+"$3"} 2>/dev/null
i=0; while [ ! -s "$OUT" ] && [ $i -lt 120 ]; do i=$((i+1)); /bin/sleep 0.5; done
if [ -s "$OUT" ]; then cat "$OUT"; echo; rm -f "$OUT"; else echo '{"error": "Календарь не ответил за минуту"}'; exit 1; fi
