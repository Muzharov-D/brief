#!/bin/bash
# Установка Огонька на Mac. Откройте двойным щелчком: появится окно Терминала, потом страница настройки в браузере.
# Повторный запуск безопасен: готовое пропускается, ваши настройки и файлы не трогаются.
ROOT="$(cd "$(dirname "$0")" && pwd)"
HOMEB="${BRIEF_HOME:-$HOME/.brief}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
clear
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mОстановлено:\033[0m %s\nЗакройте окно и запустите установку ещё раз. Если повторится, пришлите этот текст тому, кто дал вам Огонька.\n' "$*"; read -r -p "Enter, чтобы закрыть. " _; exit 1; }

echo "Огонёк: личный помощник на этом компьютере."
echo "Сейчас поставлю нужные программы (5-15 минут, один раз), потом откроется страница настройки."

[ "$(sw_vers -productVersion | cut -d. -f1)" -ge 14 ] || fail "нужна macOS 14 или новее."
# Общие ключи приложения Telegram (telegram-app.json от того, кто дал Огонька): с ними не нужно заводить
# своё приложение на my.telegram.org. Ищем рядом с установщиком и в Загрузках, кладём в ~/.brief с правами 600.
for k in "$ROOT/telegram-app.json" "$(dirname "$ROOT")/telegram-app.json" "$HOME/Downloads/telegram-app.json"; do
  [ -f "$k" ] && [ ! -f "$HOMEB/telegram-app.json" ] || continue
  id="$(plutil -extract api_id raw -o - "$k" 2>/dev/null)"; hash="$(plutil -extract api_hash raw -o - "$k" 2>/dev/null)"
  if [[ "$id" =~ ^[0-9]+$ && "$hash" =~ ^[0-9a-fA-F]{32}$ ]]; then
    mkdir -p "$HOMEB" && chmod 700 "$HOMEB" && cp "$k" "$HOMEB/telegram-app.json" && chmod 600 "$HOMEB/telegram-app.json" \
      && echo "Ключи приложения Telegram взяты из $k"
  else
    echo "Файл $k не похож на ключи Telegram, пропускаю."
  fi
done
case "$ROOT" in "$HOME/Library/Mobile Documents"*|"$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*)
  step "Переношу Огонька в вашу домашнюю папку (~/Огонёк), чтобы macOS не мешала ему работать"
  DEST="$HOME/Огонёк"
  if [ "$ROOT" != "$DEST" ]; then
    mkdir -p "$DEST" && ditto "$ROOT" "$DEST" || fail "не получилось скопировать в $DEST"
    exec /bin/bash "$DEST/Установить.command"
  fi ;;
esac

step "1/4 Снимаю пометку «скачано из интернета» и готовлю помощников Календаря и Напоминаний"
xattr -dr com.apple.quarantine "$ROOT" 2>/dev/null
mkdir -p "$HOMEB/bin" "$HOMEB/helpers" && chmod 700 "$HOMEB"
for a in "$ROOT/helpers/"*.app; do
  [ -d "$a" ] || continue
  dst="$HOMEB/helpers/$(basename "$a")"
  rm -rf "$dst"; ditto --noextattr "$a" "$dst" && xattr -cr "$dst" && codesign --force -s - "$dst" 2>/dev/null || fail "не подписался $(basename "$a")"
done
# Огонёк в Программах: находится через Spotlight, можно закрепить в Dock. Расписание работает от копии в ~/.brief/helpers
mkdir -p "$HOME/Applications" && rm -rf "$HOME/Applications/Огонёк.app" && ditto "$HOMEB/helpers/Огонёк.app" "$HOME/Applications/Огонёк.app"
cat > "$HOMEB/bin/brief" <<EOF
#!/bin/bash
exec "$ROOT/core/brief" "\$@"
EOF
chmod +x "$HOMEB/bin/brief" "$ROOT/core/brief" "$ROOT/core/codex-path" "$ROOT/core/claude-path" "$ROOT"/connectors/*/*.sh
grep -q '.brief/bin' "$HOME/.zprofile" 2>/dev/null || echo 'export PATH="$HOME/.brief/bin:$PATH"' >> "$HOME/.zprofile"

step "2/4 Python и библиотеки (Telegram, расшифровка голоса)"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
if [ ! -x "$UV" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null || fail "не скачался установщик Python (uv). Проверьте интернет."
  UV="$HOME/.local/bin/uv"
fi
PY="$HOMEB/venv/bin/python"
[ -x "$PY" ] || "$UV" venv -q --python 3.12 "$HOMEB/venv" || fail "не создалось окружение Python"
"$UV" pip install -q --python "$PY" telethon qrcode faster-whisper cryptography || fail "не поставились библиотеки"

step "3/4 Модель распознавания речи (около 0,5 ГБ, работает без интернета)"
"$PY" -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')" 2>/dev/null \
  || echo "   Не скачалась, это не страшно: голосовые заработают, когда интернет будет стабильнее."

step "4/4 Открываю страницу настройки в браузере"
echo "   Дальше всё на странице: Огонёк спросит по шагам и объяснит, что и зачем."
echo "   Это окно не закрывайте, пока не закончите настройку."
exec "$ROOT/core/brief" setup
