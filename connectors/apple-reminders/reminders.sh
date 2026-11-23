#!/bin/bash
# Напоминания macOS: reminders.sh list|done [дней] | add|complete|retime <файл.json>
DIR="$(cd "$(dirname "$0")" && pwd)"; APP="${BRIEF_HOME:-$HOME/.brief}/helpers/Brief Reminders.app"
[ -d "$APP" ] || { echo '{"error": "Нет помощника Напоминаний: запустите установку ещё раз (Установить.command)"}'; exit 1; }
OUT="$(mktemp -u -t brief-rem).json"; cmd="$1"
case "$cmd" in
  list|done) open -W -g -a "$APP" --args "$cmd" "${2:-7}" "$OUT" 2>/dev/null ;;
  add|complete|retime) IN="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"; open -W -g -a "$APP" --args "$cmd" "$IN" "$OUT" 2>/dev/null ;;
  confirm|reject|proposals) echo '{"error": "в Напоминаниях задачи другим людям не ставятся: только себе"}'; exit 1 ;;
  *) echo '{"error": "tasks list [дней] | done [дней] | add <файл> | complete <файл> | retime <файл>"}'; exit 1 ;;
esac
i=0; while [ ! -s "$OUT" ] && [ $i -lt 120 ]; do i=$((i+1)); /bin/sleep 0.5; done
if [ -s "$OUT" ]; then cat "$OUT"; echo; rm -f "$OUT"; else echo '{"error": "Напоминания не ответили за минуту"}'; exit 1; fi
