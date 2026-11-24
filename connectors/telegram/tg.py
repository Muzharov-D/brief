"""Чтение Telegram для брифинга. Только чтение: ничего не отправляет и не отмечает прочитанным.

Ключи и сессия лежат вне iCloud, в ~/.local/share/briefing-telegram (права 600).

  run tg login                 вход: api_id, api_hash, телефон, код и пароль вводятся в терминале
  run tg inbox [часов=16]      JSON: awaiting_reply (чаты, которые владелец открыл, а последнее сообщение
                              не его), unread_not_opened (только число непрочитанных чатов). Непрочитанное
                              не читается вообще: если владелец чат не открыл, агент его тоже не открывает
  run tg sent [часов=12]       мои сообщения с контекстом
  run tg links <файл.json>     ссылки «открыть чат с готовым текстом» для бота: {"items":[{"chat_id","text"}]} ->
                              {"links":[{"chat","link"}]}; нажатие открывает чат с текстом в поле, отправляет человек сам.
                              Без @username у собеседника ссылка открывает выбор чата с тем же текстом
  run tg draft <файл.json> [force]  черновики в поле ввода чатов, без отправки: {"items":[{"chat_id","text"}]};
                              force только по прямой просьбе владельца (пропускает проверку «последнее моё»)
  run tg channels [часов=168] [постов_с_канала=3] [символов=300]  самые просматриваемые посты каналов
  run tg saved [часов=24]      «Избранное»: заметки себе, ссылки, пересланное
  run tg find <имя>            chat_id личного чата или группы по имени
  run tg voices [часов=24] [папка]  скачать мои голосовые из «Избранного» (по умолчанию в DATA/voice)
  run tg birthdays             дни рождения контактов из Telegram
  run tg notify <файл>         прислать текст владельцу: через бота (если настроен run tg bot-setup, с push),
                              иначе в «Избранное». Писать можно только себе, другим отправка закрыта
  run tg bot-setup             подключить бота для брифингов: токен вводится в окне macOS
  run tg bot-inbox [папка]     забрать новое, что владелец прислал боту (голосовые, аудио, текст); только из
                              его чата, остальным бот не отвечает. Голосовые скачиваются в папку

  access                 какие папки Telegram видят агенты (окно выбора; access.json)
Правило: сообщения, которые владелец не прочитал, агенты не читают (только id <= read_inbox_max_id).
"""
import asyncio
import getpass
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient, functions
from telethon import utils as tg_utils
from telethon.tl.functions.messages import GetPeerDialogsRequest
from telethon.tl.types import Channel, Chat, User

DATA = Path(os.environ.get("BRIEF_TG_DATA") or (Path.home() / ".local/share/briefing-telegram"))
CONFIG = DATA / "config.json"
SESSION = DATA / "briefing"
BOT = DATA / "bot.json"          # {"token", "chat_id"}: бот, который присылает брифинги владельцу
BOT_INBOX = DATA / "bot_inbox.json"  # что владелец прислал боту: голосовые и заметки
NOTIFIED = DATA / "notified.json"  # id сообщений в «Избранном», которые прислал сам агент
ACCESS = DATA / "access.json"  # {"folders": ["Папка", ...]}: агентам видны только чаты из этих папок Telegram
TEXT_CHARS = 500
MAX_MESSAGES = 15


class ReadOnlyClient(TelegramClient):
    """Запрещает всё, что пишет в Telegram или меняет статус прочтения.
    Единственное исключение: команда draft кладёт черновик в поле ввода чата (SaveDraftRequest).
    Черновик ничего не отправляет: отправляет сам владелец."""

    allow_drafts = False
    allow_self_send = False  # только команда notify и только в «Избранное» (InputPeerSelf)

    def _blocked(self, *a, **k):
        raise RuntimeError("tg.py работает только на чтение")

    send_message = send_file = send_read_acknowledge = forward_messages = _blocked
    edit_message = delete_messages = pin_message = unpin_message = _blocked
    delete_dialog = edit_folder = edit_admin = edit_permissions = kick_participant = _blocked

    scope = None  # None: доступ ко всем чатам; иначе правила папок из ACCESS

    async def connect(self, *a, **k):
        await super().connect(*a, **k)
        if await self.is_user_authorized():
            if not ACCESS.exists():  # в паке без выбранных папок агентам не видно ничего
                sys.exit(json.dumps({"error": "не выбраны папки Telegram для агентов: ~/Briefing/tools/run tg access"},
                                    ensure_ascii=False))
            await self.load_scope()

    async def load_scope(self):
        """Чаты из папок access.json. Если файла нет, доступ ко всем. Если папка не найдена, ошибка, а не «всё»."""
        from telethon.tl.functions.messages import GetDialogFiltersRequest
        wanted = json.loads(ACCESS.read_text(encoding="utf-8")).get("folders") or []
        res = await self(GetDialogFiltersRequest())
        found, rules = set(), []
        for f in getattr(res, "filters", res):
            title = getattr(getattr(f, "title", None), "text", getattr(f, "title", None))
            if title not in wanted:
                continue
            found.add(title)
            ids = {tg_utils.get_peer_id(x) for x in list(getattr(f, "include_peers", []) or [])
                   + list(getattr(f, "pinned_peers", []) or [])}
            excl = {tg_utils.get_peer_id(x) for x in getattr(f, "exclude_peers", []) or []}
            rules.append((f, ids, excl))
        missing = [w for w in wanted if w not in found]
        if missing:
            raise RuntimeError("в Telegram нет папок из access.json: " + ", ".join(missing))
        self.scope = rules
        self._seen_ok = set()

    def allows(self, entity):
        """Можно ли агенту видеть этот чат. «Избранное» (свой аккаунт) можно всегда."""
        if self.scope is None:
            return True
        if isinstance(entity, User) and entity.is_self:
            return True
        pid = tg_utils.get_peer_id(entity)
        for f, ids, excl in self.scope:
            if pid in excl:
                continue
            if pid in ids:
                self._seen_ok.add(pid)
                return True
            if isinstance(entity, User):
                cat = "bots" if entity.bot else ("contacts" if entity.contact else "non_contacts")
            elif isinstance(entity, Channel) and not entity.megagroup:
                cat = "broadcasts"
            else:
                cat = "groups"
            if getattr(f, cat, False):
                self._seen_ok.add(pid)  # чат из категории папки: запросы к нему разрешены
                return True
        return False

    async def iter_dialogs(self, *a, **k):
        async for d in super().iter_dialogs(*a, **k):
            if self.allows(d.entity):
                yield d

    def _peer_ok(self, request):
        """Запрос к конкретному чату вне папок не уходит в Telegram вообще."""
        if self.scope is None:
            return True
        name = type(request).__name__
        if name == "GetMessagesRequest" and not hasattr(request, "channel"):
            return False  # сообщения по номерам без чата: обход папок, запрещено
        peers = [getattr(request, "peer", None), getattr(request, "channel", None)]
        peers += [getattr(x, "peer", None) for x in getattr(request, "peers", []) or []]
        for peer in peers:
            if peer is None or type(peer).__name__ in ("InputPeerSelf", "InputPeerEmpty"):
                continue
            try:
                pid = tg_utils.get_peer_id(peer)
            except Exception:  # noqa: BLE001
                return False
            if not any(pid in ids and pid not in excl for _, ids, excl in self.scope) \
                    and pid not in getattr(self, "_seen_ok", set()):
                return False
        return True

    async def __call__(self, request, *a, **k):
        name = type(request).__name__
        if not self._peer_ok(request):
            raise RuntimeError(f"tg.py: чат вне разрешённых папок, запрос {name} не отправлен")
        allowed = name.startswith(("Get", "Search", "Check", "Resolve", "Invoke", "Init")) or name in {
            "SendCodeRequest", "SignInRequest", "CheckPasswordRequest", "ExportAuthorizationRequest",
            "ImportAuthorizationRequest", "ImportBotAuthorizationRequest",
            "ExportLoginTokenRequest", "ImportLoginTokenRequest",
        } or (self.allow_drafts and name == "SaveDraftRequest") or (
            self.allow_self_send and name == "SendMessageRequest"
            and type(getattr(request, "peer", None)).__name__ == "InputPeerSelf")
        if not allowed:
            raise RuntimeError(f"tg.py работает только на чтение, запрос {name} запрещён")
        return await super().__call__(request, *a, **k)


def load_config():
    if not CONFIG.exists():
        sys.exit(json.dumps({"error": "Telegram не подключён: запусти run tg login"}, ensure_ascii=False))
    return json.loads(CONFIG.read_text())


def client(cfg):
    return ReadOnlyClient(str(SESSION), cfg["api_id"], cfg["api_hash"],
                          connection_retries=10, retry_delay=3, request_retries=6, timeout=20,
                          device_model="Briefing (Mac)", app_version="1.0", system_version="macOS")


def name_of(entity):
    if entity is None:
        return ""
    if isinstance(entity, User):
        n = " ".join(x for x in [entity.first_name, entity.last_name] if x)
        return n or (entity.username or str(entity.id))
    return getattr(entity, "title", "") or str(entity.id)


def kind_of(entity):
    if isinstance(entity, User):
        return "bot" if entity.bot else "личный"
    if isinstance(entity, Chat):
        return "группа"
    if isinstance(entity, Channel):
        return "канал" if entity.broadcast else "группа"
    return "?"


def local(dt):
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


async def login():
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    if CONFIG.exists():
        cfg = json.loads(CONFIG.read_text())
    else:
        print("api_id и api_hash берутся на https://my.telegram.org, API development tools. В форме: App title и Short name любые латиницей (например ogonyok), URL пустой, Platform: Desktop, Create app.")
        print("Там App api_id это число (обычно 7–9 цифр), App api_hash это строка из 32 букв и цифр.\n")
        while True:
            while True:
                digits = "".join(ch for ch in input("Вставь App api_id и нажми Enter: ") if ch.isdigit())
                if 5 <= len(digits) <= 12:
                    break
                print(f"  Это не похоже на api_id (цифр: {len(digits)}). Скопируй число из строки App api_id.")
            while True:
                api_hash = "".join(ch for ch in input("Вставь App api_hash и нажми Enter: ")
                                   if ch in "0123456789abcdefABCDEF").lower()
                if len(api_hash) == 32:
                    break
                print(f"  Это не похоже на api_hash (символов: {len(api_hash)}, нужно 32). "
                      "Скопируй строку из App api_hash.")
            print(f"\n  api_id:   {digits}\n  api_hash: {api_hash[:4]}…{api_hash[-4:]}")
            if input("  Совпадает с my.telegram.org? Enter = да, н = ввести заново: ").strip().lower() in ("", "д", "да", "y", "yes"):
                break
            print()
        cfg = {"api_id": int(digits), "api_hash": api_hash}
        CONFIG.write_text(json.dumps(cfg))
        CONFIG.chmod(0o600)
        print("  Ключи сохранены.\n")
    c = client(cfg)
    await c.start(phone=lambda: input("Телефон (+7…): ").strip(),
                  code_callback=lambda: input("Код из Telegram: ").strip(),
                  password=lambda: getpass.getpass("Облачный пароль (если есть, не отображается): "))
    me = await c.get_me()
    await c.disconnect()
    for p in DATA.glob("briefing.session*"):
        p.chmod(0o600)
    print(f"Готово, вход выполнен как {name_of(me)}. Окно можно закрыть.")


WINDOWS = os.name == "nt"


def _tk():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    return root


def dialog(text, hidden=False, default=""):
    """Окно ввода (macOS или Windows). Значение уходит прямо в скрипт и нигде не печатается."""
    import subprocess
    if WINDOWS:
        from tkinter import simpledialog
        root = _tk()
        value = simpledialog.askstring("Огонёк: вход в Telegram", text, parent=root, show="*" if hidden else None,
                                       initialvalue=default)
        root.destroy()
        if value is None:
            raise KeyboardInterrupt
        return value.strip()
    script = (f'display dialog {json.dumps(text, ensure_ascii=False)} default answer {json.dumps(default, ensure_ascii=False)}'
              f'{" with hidden answer" if hidden else ""} with title "Брифинг: вход в Telegram"'
              ' buttons {"Отмена", "OK"} default button "OK"')
    r = subprocess.run(["osascript", "-e", f"text returned of ({script})"], capture_output=True, text=True)
    if r.returncode != 0:
        raise KeyboardInterrupt
    return r.stdout.rstrip("\n")


def notify(text):
    import subprocess
    if WINDOWS:
        from tkinter import messagebox
        root = _tk()
        messagebox.showinfo("Огонёк: вход в Telegram", text, parent=root)
        root.destroy()
        return
    subprocess.run(["osascript", "-e", f'display dialog {json.dumps(text, ensure_ascii=False)} with title "Брифинг: вход в Telegram"'
                    ' buttons {"OK"} default button "OK"'], capture_output=True)


def show_qr(f):
    """QR-код для входа: на Mac в Safari, как было, на Windows в браузере по умолчанию."""
    import subprocess
    if WINDOWS:
        import webbrowser
        webbrowser.open(f.as_uri())
    else:
        subprocess.run(["open", "-a", "Safari", str(f)])


def pick_many(titles, current):
    """Выбор нескольких папок: окно macOS или Windows. Пустой список, если отменили."""
    import subprocess
    if WINDOWS:
        import tkinter as tk
        root = tk.Tk()
        root.title("Огонёк: доступ к Telegram")
        root.attributes("-topmost", True)
        tk.Label(root, text="Какие папки Telegram могут читать агенты?\nОстальные чаты им будут не видны совсем.",
                 justify="left").pack(padx=16, pady=(14, 6), anchor="w")
        lb = tk.Listbox(root, selectmode="multiple", height=min(len(titles), 14), width=48, exportselection=False)
        for i, t in enumerate(titles):
            lb.insert("end", t)
            if t in current:
                lb.selection_set(i)
        lb.pack(padx=16, pady=6, fill="both", expand=True)
        res = []

        def save():
            res.extend(titles[i] for i in lb.curselection())
            root.destroy()
        row = tk.Frame(root)
        row.pack(padx=16, pady=(6, 14), anchor="e")
        tk.Button(row, text="Отмена", command=root.destroy).pack(side="right", padx=(8, 0))
        tk.Button(row, text="Сохранить", command=save, default="active").pack(side="right")
        root.mainloop()
        return res
    items = "{" + ", ".join(json.dumps(t, ensure_ascii=False) for t in titles) + "}"
    chosen = "{" + ", ".join(json.dumps(t, ensure_ascii=False) for t in current if t in titles) + "}"
    script = (f"set r to choose from list {items} default items {chosen} with multiple selections allowed "
              'with title "Брифинг: доступ к Telegram" with prompt "Какие папки Telegram могут читать агенты? '
              'Остальные чаты им будут не видны совсем." OK button name "Сохранить" cancel button name "Отмена"\n'
              'if r is false then return ""\n'
              "set AppleScript's text item delimiters to linefeed\nreturn r as text")
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return [x for x in r.stdout.splitlines() if x.strip()]


async def gui_login():
    """Вход без терминала: ключи в окнах macOS, сам вход по QR-коду с телефона."""
    import qrcode
    import qrcode.image.svg
    import subprocess
    from telethon.errors import SessionPasswordNeededError

    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not CONFIG.exists():
        hint = "Скопируй с my.telegram.org, API development tools.\n\n"
        while True:
            digits = "".join(ch for ch in dialog(hint + "App api_id (число):") if ch.isdigit())
            if 5 <= len(digits) <= 12:
                break
            notify(f"Это не похоже на api_id: цифр {len(digits)}. Скопируй число из строки App api_id.")
        while True:
            api_hash = "".join(ch for ch in dialog(hint + "App api_hash (32 символа):")
                               if ch in "0123456789abcdefABCDEF").lower()
            if len(api_hash) == 32:
                break
            notify(f"Это не похоже на api_hash: символов {len(api_hash)}, нужно 32.")
        CONFIG.write_text(json.dumps({"api_id": int(digits), "api_hash": api_hash}))
        CONFIG.chmod(0o600)
        print("ключи сохранены")

    c = client(json.loads(CONFIG.read_text()))
    await c.connect()
    if not await c.is_user_authorized():
        qr_file = DATA / "login-qr.svg"
        try:
            for attempt in range(5):
                qr = await c.qr_login()
                qrcode.make(qr.url, image_factory=qrcode.image.svg.SvgPathFillImage, box_size=20).save(str(qr_file))
                show_qr(qr_file)
                print("QR показан, жду сканирования")
                try:
                    await qr.wait(timeout=110)
                    break
                except asyncio.TimeoutError:
                    print("QR устарел, показываю новый")
                except SessionPasswordNeededError:
                    await c.sign_in(password=dialog("У аккаунта включён облачный пароль. Введи его:", hidden=True))
                    break
            else:
                raise RuntimeError("QR не отсканирован за 5 попыток")
        finally:
            qr_file.unlink(missing_ok=True)
    me = await c.get_me()
    await c.disconnect()
    for p in DATA.glob("briefing.session*"):
        p.chmod(0o600)
    print(f"вход выполнен: {name_of(me)}")
    notify(f"Готово, вход выполнен как {name_of(me)}. Вкладку с QR-кодом в браузере можно закрыть.")


async def access_setup():
    """Какие папки Telegram видят агенты: выбор в окне macOS, сохраняется в access.json.
    Чаты вне выбранных папок агентам не видны совсем (списки, поиск, ответы бота, прямые запросы)."""
    import subprocess
    from telethon.tl.functions.messages import GetDialogFiltersRequest
    c = client(load_config())
    await TelegramClient.connect(c)  # без текущих правил: нужно увидеть все папки
    if not await c.is_user_authorized():
        sys.exit(json.dumps({"error": "Сессия Telegram недействительна: запусти run tg gui-login"}, ensure_ascii=False))
    res = await c(GetDialogFiltersRequest())
    await c.disconnect()
    titles = [getattr(getattr(f, "title", None), "text", getattr(f, "title", None))
              for f in getattr(res, "filters", res)]
    titles = [t for t in titles if t]
    if not titles:
        notify("В Telegram пока нет папок. Создай папку с чатами, которые можно видеть агентам "
               "(Настройки, Папки с чатами), и запусти выбор снова.")
        sys.exit(json.dumps({"error": "нет папок Telegram"}, ensure_ascii=False))
    current = json.loads(ACCESS.read_text(encoding="utf-8")).get("folders", []) if ACCESS.exists() else []
    picked = pick_many(titles, current)
    if not picked:
        sys.exit(json.dumps({"error": "папки не выбраны, доступ не изменён"}, ensure_ascii=False))
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    ACCESS.write_text(json.dumps({"folders": picked}, ensure_ascii=False))
    ACCESS.chmod(0o600)
    print(json.dumps({"folders": picked}, ensure_ascii=False))


async def folders_list():
    """Папки Telegram для мастера настройки: название, сколько чатов указано явно, какие категории включены."""
    from telethon.tl.functions.messages import GetDialogFiltersRequest
    c = client(load_config())
    await TelegramClient.connect(c)  # без текущих правил: нужно увидеть все папки
    if not await c.is_user_authorized():
        sys.exit(json.dumps({"error": "Сессия Telegram недействительна: войдите заново"}, ensure_ascii=False))
    res = await c(GetDialogFiltersRequest())
    await c.disconnect()
    out = []
    for f in getattr(res, "filters", res):
        t = getattr(f, "title", None)
        t = getattr(t, "text", t)
        if not t:
            continue
        cats = [n for n, flag in (("контакты", "contacts"), ("не контакты", "non_contacts"), ("группы", "groups"),
                                  ("каналы", "broadcasts"), ("боты", "bots")) if getattr(f, flag, False)]
        out.append({"title": t, "chats": len(getattr(f, "include_peers", []) or []) + len(getattr(f, "pinned_peers", []) or []),
                    "categories": cats})
    current = json.loads(ACCESS.read_text(encoding="utf-8")).get("folders", []) if ACCESS.exists() else []
    print(json.dumps({"folders": out, "selected": current}, ensure_ascii=False))


def access_save(titles):
    """Сохранить выбранные папки (мастер настройки). Пустой выбор не сохраняем: агенты тогда не видят ничего."""
    titles = [t for t in titles if t.strip()]
    if not titles:
        sys.exit(json.dumps({"error": "папки не выбраны, доступ не изменён"}, ensure_ascii=False))
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    ACCESS.write_text(json.dumps({"folders": titles}, ensure_ascii=False), encoding="utf-8")
    ACCESS.chmod(0o600)
    print(json.dumps({"folders": titles}, ensure_ascii=False))


def status():
    """Что уже настроено, без обращения к Telegram."""
    folders = json.loads(ACCESS.read_text(encoding="utf-8")).get("folders", []) if ACCESS.exists() else []
    print(json.dumps({"keys": CONFIG.exists(), "session": Path(str(SESSION) + ".session").exists(),
                      "bot": BOT.exists(), "folders": folders}, ensure_ascii=False))


async def inbox(hours):
    cfg = load_config()
    c = client(cfg)
    await c.connect()
    if not await c.is_user_authorized():
        sys.exit(json.dumps({"error": "Сессия Telegram недействительна: запусти run tg login"}, ensure_ascii=False))
    me = await c.get_me()
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    awaiting, unread_chats = [], 0

    async def recent(entity, n, read_max):
        # только то, что владелец уже видел: входящие с id <= read_inbox_max_id
        msgs = []
        async for m in c.iter_messages(entity, limit=n, max_id=read_max + 1):
            sender = me if m.out else await m.get_sender()
            msgs.append({"at": local(m.date), "from": "я" if m.out else name_of(sender),
                         "text": (m.message or ("[медиа]" if m.media else ""))[:TEXT_CHARS],
                         "mentions_me": bool(m.mentioned)})
        return list(reversed(msgs))

    async for d in c.iter_dialogs(archived=False):
        last = d.message
        if last is None or last.date < since - timedelta(days=2):
            continue
        entity = d.entity
        kind = kind_of(entity)
        is_muted = bool(d.dialog.notify_settings and d.dialog.notify_settings.mute_until
                        and d.dialog.notify_settings.mute_until > datetime.now(timezone.utc))
        mentions = d.unread_mentions_count
        base = {"chat": name_of(entity), "chat_id": d.id, "type": kind, "unread": d.unread_count,
                "mentions": mentions, "last_at": local(last.date)}

        if kind == "канал" or getattr(entity, "id", None) == 777000:  # каналы и служебный аккаунт Telegram
            continue
        if kind == "bot":
            continue

        if d.unread_count or d.dialog.unread_mark:
            if not is_muted:
                unread_chats += 1  # не открыт владельцем: не читаем
            continue
        if kind == "личный" and not last.out and last.date >= since:
            awaiting.append({**base, "messages": await recent(entity, 5, d.dialog.read_inbox_max_id)})

    await c.disconnect()
    print(json.dumps({"since": local(since), "awaiting_reply": awaiting,
                      "unread_not_opened": unread_chats}, ensure_ascii=False, indent=2))


async def sent(hours):
    """Мои сообщения за последние часы: обещания и договорённости для вечернего разбора."""
    cfg = load_config()
    c = client(cfg)
    await c.connect()
    if not await c.is_user_authorized():
        sys.exit(json.dumps({"error": "Сессия Telegram недействительна: запусти run tg gui-login"}, ensure_ascii=False))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    chats = []
    async for d in c.iter_dialogs(archived=False):
        if d.message is None or d.message.date < since:
            if d.pinned:
                continue  # закреплённые идут первыми независимо от даты
            break  # остальные диалоги идут от свежих к старым
        kind = kind_of(d.entity)
        if kind in ("канал", "bot") or getattr(d.entity, "id", None) == 777000:
            continue
        mine, context = [], []
        read_max = d.dialog.read_inbox_max_id
        async for m in c.iter_messages(d.entity, limit=40):
            if m.date < since:
                break
            if not m.out and m.id > read_max:
                continue  # владелец ещё не прочитал: не читаем
            entry = {"at": local(m.date), "from": "я" if m.out else name_of(await m.get_sender()),
                     "text": (m.message or ("[медиа]" if m.media else ""))[:TEXT_CHARS]}
            context.append(entry)
            if m.out and m.message:
                mine.append(entry)
        if mine:
            chats.append({"chat": name_of(d.entity), "chat_id": d.id, "type": kind,
                          "messages": list(reversed(context))[-12:]})
    await c.disconnect()
    print(json.dumps({"since": local(since), "chats_with_my_messages": chats}, ensure_ascii=False, indent=2))


async def connected():
    c = client(load_config())
    await c.connect()
    if not await c.is_user_authorized():
        sys.exit(json.dumps({"error": "Сессия Telegram недействительна: запусти run tg gui-login"}, ensure_ascii=False))
    return c


async def channels(hours, top=3, chars=300):
    """Посты каналов (в том числе без звука) за последние часы: для недельного дайджеста.
    С каждого канала берётся top самых просматриваемых постов, текст обрезается до chars."""
    c = await connected()
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    async for d in c.iter_dialogs(archived=None):
        if kind_of(d.entity) != "канал" or d.message is None or d.message.date < since:
            continue
        posts = []
        async for m in c.iter_messages(d.entity, limit=60):
            if m.date < since:
                break
            if m.message:
                posts.append({"at": local(m.date), "views": m.views or 0, "text": m.message[:chars],
                              "link": f"https://t.me/{d.entity.username}/{m.id}" if getattr(d.entity, "username", None) else ""})
        if posts:
            total = len(posts)
            posts = sorted(posts, key=lambda p: p["views"], reverse=True)[:top]
            out.append({"channel": name_of(d.entity), "archived": d.archived, "posts_total": total,
                        "posts": sorted(posts, key=lambda p: p["at"])})
    await c.disconnect()
    print(json.dumps({"since": local(since), "channels": out}, ensure_ascii=False, indent=2))


async def saved(hours):
    """«Избранное» за последние часы: заметки себе, ссылки, пересланное, голосовые (только отметка)."""
    c = await connected()
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    items = []
    own = set(json.loads(NOTIFIED.read_text())) if NOTIFIED.exists() else set()
    async for m in c.iter_messages("me", limit=200):
        if m.date < since:
            break
        fwd = ""
        if m.fwd_from:
            fwd = getattr(m.fwd_from, "from_name", None) or ""
            if not fwd and m.fwd_from.from_id:
                try:
                    fwd = name_of(await c.get_entity(m.fwd_from.from_id))
                except Exception:
                    fwd = "переслано"
        if m.id in own:
            continue  # брифинги и разборы, присланные агентом, не заметки
        items.append({"id": m.id, "at": local(m.date), "text": (m.message or "")[:1500], "forwarded_from": fwd,
                      "voice": bool(m.voice), "media": bool(m.media) and not m.voice,
                      "links": [e.url for e in (m.entities or []) if getattr(e, "url", None)]})
    await c.disconnect()
    print(json.dumps({"since": local(since), "saved": list(reversed(items))}, ensure_ascii=False, indent=2))


async def find(query):
    """Поиск личного чата или группы по имени: chat_id для черновика."""
    c = await connected()
    q = query.lower()
    hits = []
    async for d in c.iter_dialogs(archived=None, limit=1000):
        if kind_of(d.entity) in ("канал", "bot"):
            continue
        n = name_of(d.entity)
        username = getattr(d.entity, "username", None) or ""
        if q in n.lower() or (username and q in username.lower()):
            hits.append({"chat": n, "chat_id": d.id, "type": kind_of(d.entity), "username": username,
                         "last_at": local(d.message.date) if d.message else ""})
        if len(hits) >= 10:
            break
    await c.disconnect()
    print(json.dumps({"query": query, "chats": hits}, ensure_ascii=False, indent=2))


async def voices(hours, out_dir):
    """Скачивает мои голосовые из «Избранного» за последние часы в out_dir (для локальной расшифровки).
    Уже скачанные (файл с тем же id) пропускает."""
    c = await connected()
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = []
    async for m in c.iter_messages("me", limit=200):
        if m.date < since:
            break
        if not m.voice:
            continue
        target = out / f"voice-{m.id}.ogg"
        if not target.exists():
            await m.download_media(file=str(target))
        files.append({"id": m.id, "at": local(m.date), "file": str(target),
                      "seconds": getattr(m.voice.attributes[0], "duration", None) if m.voice.attributes else None})
    await c.disconnect()
    print(json.dumps({"since": local(since), "voices": list(reversed(files))}, ensure_ascii=False, indent=2))


async def birthdays():
    """Дни рождения контактов из Telegram (если человек их указал)."""
    c = await connected()
    res = await c(functions.contacts.GetBirthdaysRequest())
    users = {u.id: u for u in res.users}
    out = []
    for b in res.contacts:
        u = users.get(b.contact_id)
        if u is None or not c.allows(u):
            continue  # контакт вне разрешённых папок
        out.append({"name": name_of(u), "chat_id": b.contact_id, "username": getattr(u, "username", None) or "",
                    "day": b.birthday.day, "month": b.birthday.month, "year": b.birthday.year})
    await c.disconnect()
    out.sort(key=lambda x: (x["month"], x["day"]))
    print(json.dumps({"birthdays": out}, ensure_ascii=False, indent=2))


MAX_DRAFTS = 8


async def links(path):
    """Ссылки t.me с готовым текстом: надёжнее черновиков, которые клиент Telegram перезаписывает."""
    from urllib.parse import quote
    items = json.loads(Path(path).read_text(encoding="utf-8")).get("items", [])[:MAX_DRAFTS]
    wanted = {int(i["chat_id"]): (i.get("text") or "").strip()[:2000] for i in items if i.get("chat_id")}
    c = await connected()
    res = []
    try:
        async for d in c.iter_dialogs(archived=None, limit=500):
            if d.id not in wanted:
                continue
            text, uname = wanted.pop(d.id), getattr(d.entity, "username", None)
            if uname:
                link = f"https://t.me/{uname}?text={quote(text)}"
            else:
                link = f"https://t.me/share/url?url={quote(text)}"
            res.append({"chat": name_of(d.entity), "chat_id": d.id, "link": link, "direct": bool(uname)})
            if not wanted:
                break
    finally:
        await c.disconnect()
    print(json.dumps({"links": res, "not_found": list(wanted)}, ensure_ascii=False, indent=2))


def chunks(text, size=3900):
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > size and cur:
            parts.append(cur); cur = ""
        cur += line
    if cur.strip():
        parts.append(cur)
    return parts


def bot_call(token, method, payload):
    import urllib.request
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    import time
    import urllib.error
    last = None
    for attempt in range(4):  # связь с api.telegram.org рвётся на рукопожатии TLS: повторяем
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except Exception:
                return {"ok": False, "description": str(e)}
        except Exception as e:  # таймаут, сброс соединения, DNS
            last = e
            time.sleep(3 * (attempt + 1))
    return {"ok": False, "description": f"нет связи с Telegram после 4 попыток: {last}"}


def strip_tags(t):
    import html as h, re
    return h.unescape(re.sub(r"</?(b|i|u|s|code|pre|a)[^>]*>", "", t))


async def send_to_me(path):
    """Прислать текст владельцу. Разметка: HTML Telegram (<b>, <i>, <a href>). Через бота, если он
    настроен (приходит push), иначе в «Избранное». Никому другому отправить нельзя."""
    text = Path(path).read_text().strip()
    if not text:
        sys.exit(json.dumps({"error": "пустой текст"}, ensure_ascii=False))
    parts = chunks(text)
    if BOT.exists():
        cfg = json.loads(BOT.read_text())
        sent_n = 0
        for part in parts:
            r = bot_call(cfg["token"], "sendMessage", {"chat_id": cfg["chat_id"], "text": part,
                                                       "parse_mode": "HTML", "disable_web_page_preview": True})
            if not r.get("ok"):
                r = bot_call(cfg["token"], "sendMessage", {"chat_id": cfg["chat_id"], "text": strip_tags(part),
                                                           "disable_web_page_preview": True})
            if not r.get("ok"):
                sys.exit(json.dumps({"error": f"бот: {r.get('description')}"}, ensure_ascii=False))
            sent_n += 1
        print(json.dumps({"sent": sent_n, "via": "бот"}, ensure_ascii=False))
        return
    from telethon.extensions import html as tg_html
    from telethon.tl.types import InputPeerSelf
    c = await connected()
    own = json.loads(NOTIFIED.read_text()) if NOTIFIED.exists() else []
    c.allow_self_send = True
    try:
        for part in parts:
            try:
                msg, ents = tg_html.parse(part)
            except Exception:
                msg, ents = strip_tags(part), None
            upd = await c(functions.messages.SendMessageRequest(peer=InputPeerSelf(), message=msg,
                                                                entities=ents or None, no_webpage=True))
            for u in getattr(upd, "updates", []) or []:
                mid = getattr(getattr(u, "message", None), "id", None) or (getattr(u, "id", None) if type(u).__name__ == "UpdateMessageID" else None)
                if mid:
                    own.append(mid)
            if getattr(upd, "id", None):
                own.append(upd.id)
    finally:
        c.allow_self_send = False
        await c.disconnect()
    NOTIFIED.write_text(json.dumps(sorted(set(own))[-500:]))
    os.chmod(NOTIFIED, 0o600)
    print(json.dumps({"sent": len(parts), "via": "Избранное"}, ensure_ascii=False))


async def bot_inbox(out_dir):
    """Новые сообщения владельца боту. Подтверждает их у Telegram (offset) только после записи в BOT_INBOX,
    поэтому ничего не теряется, даже если расшифровка потом упадёт."""
    if not BOT.exists():
        sys.exit(json.dumps({"error": "бот не настроен: run tg bot-setup"}, ensure_ascii=False))
    import urllib.request
    cfg = json.loads(BOT.read_text())
    store = json.loads(BOT_INBOX.read_text()) if BOT_INBOX.exists() else {"offset": 0, "items": []}
    upd = bot_call(cfg["token"], "getUpdates", {"offset": store["offset"], "timeout": 0,
                                                "allowed_updates": ["message"]})
    if not upd.get("ok"):
        sys.exit(json.dumps({"error": f"бот: {upd.get('description')}"}, ensure_ascii=False))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    new = []
    if upd.get("result"):
        warn_shared(cfg, store, upd["result"][0]["update_id"])
    for u in upd.get("result", []):
        store["offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if (m.get("chat") or {}).get("id") != cfg["chat_id"] or m.get("text", "").startswith("/start"):
            continue  # чужие и служебные сообщения не берём
        at = datetime.fromtimestamp(m["date"], timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
        fwd = (m.get("forward_origin") or {})
        fwd_name = (fwd.get("sender_user") or {}).get("first_name") or fwd.get("sender_user_name") or \
                   (fwd.get("chat") or {}).get("title") or ""
        item = {"id": m["message_id"], "at": at, "forwarded_from": fwd_name}
        rep = m.get("reply_to_message") or {}
        if rep:  # ответ на сообщение бота (брифинг, список задач): агенту видно, о чём речь
            item["reply_to"] = (rep.get("text") or rep.get("caption") or "")[:1500]
        media = m.get("voice") or m.get("audio") or m.get("video_note")
        if media:
            f = bot_call(cfg["token"], "getFile", {"file_id": media["file_id"]})
            if not f.get("ok"):
                item.update(kind="voice", error=f.get("description"))
            else:
                ext = Path(f["result"]["file_path"]).suffix or ".ogg"
                target = out / f"bot-{m['message_id']}{ext}"
                urllib.request.urlretrieve(
                    f"https://api.telegram.org/file/bot{cfg['token']}/{f['result']['file_path']}", target)
                item.update(kind="voice", file=str(target), seconds=media.get("duration"))
        elif m.get("text") or m.get("caption"):
            item.update(kind="text", text=(m.get("text") or m.get("caption"))[:4000])
        else:
            continue
        new.append(item)
    store["items"] = (store["items"] + new)[-300:]
    if os.environ.get("BRIEF_DRY"):  # пробный прогон: смотрим, но не забираем, настоящий агент получит то же
        print(json.dumps({"new": new, "items": store["items"]}, ensure_ascii=False, indent=2))
        return
    BOT_INBOX.write_text(json.dumps(store, ensure_ascii=False))
    os.chmod(BOT_INBOX, 0o600)
    if upd.get("result"):
        bot_call(cfg["token"], "getUpdates", {"offset": store["offset"], "timeout": 0})
    print(json.dumps({"new": new, "items": store["items"]}, ensure_ascii=False, indent=2))


OTHER_BOT_FILES = [Path.home() / ".local/share/briefing-telegram/bot.json"]  # прежний брифинг и пак на Codex


def bot_elsewhere(token):
    """Где ещё на этом Mac или в сети уже работает этот бот: другая установка брифинга или вебхук сервиса."""
    for f in OTHER_BOT_FILES:
        try:
            if f.resolve() != BOT.resolve() and json.loads(f.read_text()).get("token") == token:
                return f"брифинг из {f.parent}"
        except (OSError, ValueError):
            pass
    hook = bot_call(token, "getWebhookInfo", {})
    if (hook.get("result") or {}).get("url"):
        return "сервис в интернете (у бота настроен вебхук)"
    return ""


def warn_shared(cfg, store, first_id):
    """Номера обновлений у бота идут подряд. Пропуск значит, что сообщения забрала другая программа."""
    if not store.get("offset") or first_id <= store["offset"] or os.environ.get("BRIEF_DRY"):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if store.get("shared_warned") == today:
        return
    store["shared_warned"] = today
    bot_call(cfg["token"], "sendMessage", {"chat_id": cfg["chat_id"], "text":
             "Похоже, этого бота читает ещё одна программа, например прежний брифинг: часть ваших сообщений "
             "до меня не дошла. Чтобы ничего не терялось, заведите мне отдельного бота: brief setup, шаг Telegram."})


async def bot_setup():
    """Бот для брифингов: владелец создаёт его в @BotFather, токен вводит в окне macOS (не печатается),
    затем жмёт Start в боте. Сохраняется только чат владельца, проверка по его id."""
    c = await connected()
    me = await c.get_me()
    await c.disconnect()
    token = dialog("Токен бота от @BotFather (вставь и нажми OK):", hidden=True).strip()
    info = bot_call(token, "getMe", {})
    if not info.get("ok"):
        sys.exit(json.dumps({"error": "токен не подошёл"}, ensure_ascii=False))
    username = info["result"]["username"]
    busy = bot_elsewhere(token)
    if busy:
        sys.exit(json.dumps({"error": f"@{username} уже работает в другой программе: {busy}. Два брифинга на одном "
                                      "боте теряют сообщения друг друга. Создайте нового бота в @BotFather "
                                      "и вставьте его токен."}, ensure_ascii=False))
    import webbrowser
    webbrowser.open(f"https://t.me/{username}?start=brief")
    notify(f"В Telegram откроется @{username}. Нажми в нём «Start» (Запустить), потом OK здесь.")
    chat_id = None
    for _ in range(3):
        upd = bot_call(token, "getUpdates", {"timeout": 20})
        for u in upd.get("result", []):
            m = u.get("message") or {}
            if (m.get("from") or {}).get("id") == me.id and (m.get("chat") or {}).get("type") == "private":
                chat_id = m["chat"]["id"]
        if chat_id:
            break
    if not chat_id:
        sys.exit(json.dumps({"error": f"не вижу нажатия Start от тебя в @{username}, запусти bot-setup ещё раз"}, ensure_ascii=False))
    BOT.write_text(json.dumps({"token": token, "chat_id": chat_id, "username": username}))
    os.chmod(BOT, 0o600)
    bot_call(token, "sendMessage", {"chat_id": chat_id, "text": "Готово. Сюда будут приходить брифинги."})
    print(json.dumps({"ok": True, "bot": username}, ensure_ascii=False))


async def draft(path, force=False):
    """Черновики ответов: текст кладётся в поле ввода чата на всех устройствах, ничего не отправляется.
    Вход: {"items":[{"chat_id": <из inbox>, "text": "..."}]}. Пропускает чат, если там уже есть
    черновик, если последнее сообщение моё (уже ответил), а также каналы, ботов и «Избранное»."""
    items = json.loads(Path(path).read_text()).get("items", [])[:MAX_DRAFTS]
    c = client(load_config())
    await c.connect()
    if not await c.is_user_authorized():
        sys.exit(json.dumps({"error": "Сессия Telegram недействительна: запусти run tg gui-login"}, ensure_ascii=False))
    me = await c.get_me()
    wanted = {int(i["chat_id"]): (i.get("text") or "").strip()[:4000] for i in items if i.get("chat_id")}
    saved, skipped = [], []
    c.allow_drafts = True
    try:
        async for d in c.iter_dialogs(archived=False, limit=300):
            if d.id not in wanted:
                continue
            text, name = wanted.pop(d.id), name_of(d.entity)
            kind = kind_of(d.entity)
            existing = getattr(d.draft, "text", "") if d.draft else ""
            if not text:
                skipped.append({"chat": name, "why": "пустой текст"})
            elif kind in ("канал", "bot") or d.id == me.id:
                skipped.append({"chat": name, "why": f"тип {kind}"})
            elif existing:
                skipped.append({"chat": name, "why": "в чате уже есть черновик"})
            elif d.unread_count or d.dialog.unread_mark:
                skipped.append({"chat": name, "why": "чат не прочитан владельцем"})
            elif d.message is not None and d.message.out and not force:
                skipped.append({"chat": name, "why": "последнее сообщение уже моё"})
            else:
                await d.draft.set_message(text)
                saved.append((name, d.entity))
            if not wanted:
                break
        # Открытый в приложении чат перезаписывает черновик своим пустым полем ввода
        # (так однажды за минуту стёрся черновик). Перечитываем с сервера.
        if saved:
            await asyncio.sleep(20)
            r = await c(GetPeerDialogsRequest(peers=[e for _, e in saved]))
            kept = {tg_utils.get_peer_id(dl.peer): getattr(dl.draft, "message", "") for dl in r.dialogs}
            lost = [n for n, e in saved if not kept.get(tg_utils.get_peer_id(e))]
            skipped += [{"chat": n, "why": "черновик стёрт приложением: закрой этот чат в Telegram и повтори"}
                        for n in lost]
            saved = [(n, e) for n, e in saved if n not in lost]
    finally:
        c.allow_drafts = False
        await c.disconnect()
    skipped += [{"chat_id": k, "why": "чат не найден"} for k in wanted]
    print(json.dumps({"drafts_saved": [n for n, _ in saved], "skipped": skipped}, ensure_ascii=False, indent=2))


def _main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "inbox"
    if cmd == "login":
        try:
            asyncio.run(login())
        except (KeyboardInterrupt, EOFError):
            print("\nВход прерван.")
        except Exception as e:
            print(f"\nОшибка: {e}")
        input("\nНажми Enter, чтобы закрыть окно.")
    elif cmd == "gui-login":
        try:
            asyncio.run(gui_login())
        except KeyboardInterrupt:
            sys.exit("вход отменён")
    elif cmd == "sent":
        asyncio.run(sent(float(sys.argv[2]) if len(sys.argv) > 2 else 12))
    elif cmd == "links" and len(sys.argv) > 2:
        asyncio.run(links(sys.argv[2]))
    elif cmd == "draft" and len(sys.argv) > 2:
        # force: черновик по прямой просьбе владельца, даже если последнее сообщение его; отправки нет и тут
        asyncio.run(draft(sys.argv[2], force=len(sys.argv) > 3 and sys.argv[3] == "force"))
    elif cmd == "channels":
        asyncio.run(channels(float(sys.argv[2]) if len(sys.argv) > 2 else 168,
                             int(sys.argv[3]) if len(sys.argv) > 3 else 3, int(sys.argv[4]) if len(sys.argv) > 4 else 300))
    elif cmd == "saved":
        asyncio.run(saved(float(sys.argv[2]) if len(sys.argv) > 2 else 24))
    elif cmd == "find" and len(sys.argv) > 2:
        asyncio.run(find(" ".join(sys.argv[2:])))
    elif cmd == "voices":
        asyncio.run(voices(float(sys.argv[2]) if len(sys.argv) > 2 else 24,
                           sys.argv[3] if len(sys.argv) > 3 else str(DATA / "voice")))
    elif cmd == "notify" and len(sys.argv) > 2:
        asyncio.run(send_to_me(sys.argv[2]))
    elif cmd == "bot-inbox":
        asyncio.run(bot_inbox(sys.argv[2] if len(sys.argv) > 2 else str(DATA / "voice")))
    elif cmd == "folders":
        asyncio.run(folders_list())
    elif cmd == "access-save":
        access_save(sys.argv[2:])
    elif cmd == "status":
        status()
    elif cmd == "access":
        asyncio.run(access_setup())
    elif cmd == "bot-setup":
        try:
            asyncio.run(bot_setup())
        except KeyboardInterrupt:
            sys.exit("отменено")
    elif cmd == "birthdays":
        asyncio.run(birthdays())
    elif cmd == "inbox":
        asyncio.run(inbox(float(sys.argv[2]) if len(sys.argv) > 2 else 16))
    else:
        sys.exit(__doc__)


READ_ONLY = {"inbox", "sent", "saved", "voices", "channels", "find", "birthdays"}

if __name__ == "__main__":
    # связь с Telegram время от времени рвётся (сброс соединения, таймаут): чтение повторяем,
    # а вместо трейсбека отдаём JSON с ошибкой, чтобы агенты и voice.py её поняли
    _cmd = sys.argv[1] if len(sys.argv) > 1 else "inbox"
    _tries = 3 if _cmd in READ_ONLY else 1
    for _i in range(_tries):
        try:
            _main()
            break
        except (ConnectionError, OSError, asyncio.TimeoutError) as _e:
            if _i == _tries - 1:
                print(json.dumps({"error": f"нет связи с Telegram: {_e}"}, ensure_ascii=False))
                sys.exit(1)
            import time
            time.sleep(10 * (_i + 1))
