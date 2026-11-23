#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Задачи Битрикс24 для брифинга. Агенты зовут `brief run tasks <команда>`, для пользователей
Битрикс24 это `python bitrix.py <команда>`. Каждая команда печатает один JSON; при ошибке
{"error": "..."} и код выхода 1. Ссылка вебхука и её ключ никогда не печатаются.

  setup                    спросить ссылку входящего вебхука в скрытом окне, проверить права, сохранить
  list [дней=7]            мои открытые задачи: today, due (срок до +N дней и просроченные), agent_lists_undated
                           (без срока из списков агента), open_by_list; delegated: открытые задачи, которые я
                           поставил другим (кто отвечает, срок, на сколько дней просрочено, статус)
  done [дней=7]            мои задачи, закрытые за N дней
  add <вход.json>          {"items":[{"list","title","notes","due":"ГГГГ-ММ-ДД","time":"ЧЧ:ММ"?,"key",
                           "priority":"high"?,"assignee":"<имя>"?}]}. Без assignee (или если это я) задача
                           ставится мне сразу; повтор key пропускается. Задачи другим НЕ ставятся, а
                           попадают в пронумерованные предложения: Битрикс уведомит человека от имени
                           владельца, поэтому сначала его «ставь N» в боте.
  proposals                ждущие подтверждения предложения
  confirm <N> [N...]       поставить предложенные задачи этим людям
  reject <N> [N...]        отказаться от предложений
  complete <вход.json>     {"items":[{"title","id"?,"list"?}]} закрыть открытые задачи, где я ответственный
                           или постановщик, по id или точному заголовку; не больше 10 за раз
  users <запрос>           поиск сотрудников: id, имя, должность
  doctor                   короткая проверка: подключено ли, работает ли вебхук

Ключ лежит в $BRIEF_HOME/secrets/bitrix.json (по умолчанию ~/.brief, права 600, папка 700),
состояние в $BRIEF_HOME/state/. Только стандартная библиотека, совместимо с Python 3.9.
"""
import getpass
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

AGENT_LISTS = ["Из созвонов", "Из переписки", "Жду от других"]
DEFAULT_LIST = "Битрикс"
BRIEF_LIST = "Брифинг"
KEY_PREFIX = "key: "
END_OF_DAY = (19, 0)          # срок без времени ставится на конец рабочего дня
NO_TIME = {(19, 0), (23, 59)}  # такое время считаем «без времени» при чтении
PAGE = 50
MAX_TASKS = 500
MAX_ADD = 50
MAX_COMPLETE = 10
TIMEOUT = 30
RETRIES = 2
WRITE_METHODS = {"tasks.task.add", "tasks.task.complete"}
SELF_WORDS = {"я", "мне", "меня", "себе", "сам", "сама", "мой", "моя"}
SELECT = ["ID", "TITLE", "DESCRIPTION", "DEADLINE", "STATUS", "RESPONSIBLE_ID", "CREATED_BY",
          "CLOSED_DATE", "TAGS", "PRIORITY"]
STATUS_TEXT = {1: "новая", 2: "ждёт выполнения", 3: "выполняется", 4: "ждёт контроля",
               5: "завершена", 6: "отложена", 7: "отклонена"}
SCOPE_HELP = "создай вебхук заново с правами Задачи и Пользователи"
NOT_CONFIGURED = "Битрикс24 ещё не подключён: нужна настройка (команда setup)"
BAD_KEY = ("Битрикс24 не принял ключ вебхука: возможно, вебхук удалён или ссылка скопирована "
           "не полностью. Запусти настройку Битрикс24 заново")
WEBHOOK_RE = re.compile(r"^(https?)://([^/\s?#]+)/rest/(\d+)/([A-Za-z0-9_]+)(?:/.*)?$")

_SECRETS = []


class Fail(Exception):
    """Ошибка с понятным текстом для пользователя."""


class _Retry(Exception):
    def __init__(self, message, safe):
        Exception.__init__(self, message)
        self.safe = safe  # запрос точно не дошёл до Битрикса, повтор не создаст дубль


# ---------- вывод и файлы ----------

def scrub(text):
    for s in _SECRETS:
        if s:
            text = text.replace(s, "***")
    return text


def out(obj, code=0, compact=False):
    text = json.dumps(obj, ensure_ascii=False, indent=None if compact else 2, default=str)
    sys.stdout.write(scrub(text) + "\n")
    sys.stdout.flush()
    sys.exit(code)


def home():
    return os.path.expanduser(os.environ.get("BRIEF_HOME") or "~/.brief")


def secrets_path():
    return os.path.join(home(), "secrets", "bitrix.json")


def state_path(name):
    return os.path.join(home(), "state", name)


def private_dir(path):
    os.makedirs(home(), mode=0o700, exist_ok=True)
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def write_private(path, obj):
    private_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def load_state(name, default):
    path = state_path(name)
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        raise Fail("Файл состояния «%s» повреждён: удали его или восстанови из копии" % name)


def read_input(args, usage):
    if not args:
        raise Fail("Нужен путь к файлу: " + usage)
    path = args[0]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        raise Fail("Не удалось прочитать файл «%s»" % os.path.basename(path))
    except ValueError:
        raise Fail("Файл «%s» не в формате JSON" % os.path.basename(path))
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise Fail("В файле нужен список задач: {\"items\": [...]}")
    return [i for i in items if isinstance(i, dict)]


def parse_days(args, default=7):
    if not args:
        return default
    try:
        n = int(args[0])
    except ValueError:
        raise Fail("Число дней должно быть целым, например 7")
    if n < 0 or n > 366:
        raise Fail("Число дней должно быть от 0 до 366")
    return n


# ---------- вебхук и запросы ----------

def parse_webhook(url):
    url = (url or "").strip().strip("\"'").strip()
    m = WEBHOOK_RE.match(url)
    if not m or len(m.group(4)) < 6:
        raise Fail("Это не похоже на ссылку входящего вебхука. Она выглядит так: "
                   "https://<портал>.bitrix24.ru/rest/<число>/<ключ>/ (скопируй её целиком из Битрикс24)")
    scheme, host, uid, key = m.groups()
    hostname = host.split(":")[0].lower()
    if scheme == "http" and hostname not in ("127.0.0.1", "localhost"):
        raise Fail("Ссылка вебхука должна начинаться с https://")
    webhook = "%s://%s/rest/%s/%s/" % (scheme, host, uid, key)
    for s in (webhook, key):
        if s not in _SECRETS:
            _SECRETS.append(s)
    return {"webhook": webhook, "user_id": int(uid), "key": key, "portal": host, "scheme": scheme}


def explain(code, desc, method):
    c = (code or "").lower()
    if c in ("insufficient_scope", "error_method_not_found", "method_not_found"):
        if method.startswith("tasks."):
            need = "Задачи"
        elif method.startswith("user."):
            need = "Пользователи"
        else:
            need = "Задачи и Пользователи"
        return "вебхуку не хватает прав «%s»: %s" % (need, SCOPE_HELP)
    if c in ("no_auth_found", "invalid_token", "expired_token", "invalid_credentials",
             "wrong_auth_type", "authorization_error", "invalid_grant"):
        return BAD_KEY
    if c == "access_denied":
        return "Битрикс24 запретил это действие: не хватает прав в самом портале"
    detail = (desc or code or "").strip()[:200]
    return "Битрикс24 вернул ошибку: %s" % detail


class Bitrix(object):
    def __init__(self, webhook):
        p = parse_webhook(webhook)
        self.base = p["webhook"]
        self.portal = p["portal"]
        self.scheme = p["scheme"]
        try:
            self.pause = float(os.environ.get("BRIEF_BITRIX_PAUSE", "0.5"))
        except ValueError:
            self.pause = 0.5
        self.last = 0.0

    def _throttle(self):
        # у Битрикс24 предел 2 запроса в секунду
        wait = self.last + self.pause - time.time()
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()

    def call(self, method, params=None):
        write = method in WRITE_METHODS
        attempt = 0
        while True:
            self._throttle()
            try:
                return self._once(method, params or {})
            except _Retry as e:
                attempt += 1
                if attempt > RETRIES or (write and not e.safe):
                    raise Fail(str(e))
                time.sleep(self.pause * 2 * attempt)

    def _once(self, method, params):
        req = urllib.request.Request(
            self.base + method + ".json", data=json.dumps(params).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "brief-bitrix/1.0"})
        timeout_msg = "Битрикс24 не ответил за %d секунд. Попробуй позже" % TIMEOUT
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                status, body = r.status, r.read()
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                body = e.read() or b""
            except Exception:  # noqa: BLE001
                body = b""
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise _Retry(timeout_msg, safe=False)
            raise _Retry("Нет связи с Битрикс24 (%s): проверь интернет и адрес портала" % self.portal, safe=True)
        except (socket.timeout, TimeoutError):
            raise _Retry(timeout_msg, safe=False)
        except (http.client.HTTPException, ConnectionError, OSError):
            raise _Retry("Связь с Битрикс24 оборвалась. Попробуй позже", safe=False)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict) and payload.get("error"):
            code = str(payload.get("error"))
            if code.upper() == "QUERY_LIMIT_EXCEEDED":
                raise _Retry("Битрикс24 просит делать запросы реже. Попробуй через минуту", safe=True)
            raise Fail(explain(code, str(payload.get("error_description") or ""), method))
        if status >= 500:
            raise _Retry("Битрикс24 временно недоступен (код %d). Попробуй позже" % status, safe=status == 503)
        if status in (401, 403):
            raise Fail(BAD_KEY)
        if status >= 400 or not isinstance(payload, dict):
            raise Fail("Битрикс24 ответил непонятно (код %d): проверь адрес портала в ссылке вебхука" % status)
        return payload


class Ctx(object):
    def __init__(self, cfg):
        self.bx = Bitrix(cfg["webhook"])
        self.me = int(cfg["user_id"])
        self.user_name = cfg.get("user_name") or ""
        self.portal = cfg.get("portal") or self.bx.portal
        self._names = {}

    def url(self, tid):
        return "%s://%s/company/personal/user/%d/tasks/task/view/%s/" % (self.bx.scheme, self.portal, self.me, tid)

    def user_name_by_id(self, uid):
        if uid in self._names:
            return self._names[uid]
        name = ""
        if len(self._names) < 30:
            try:
                res = self.bx.call("user.get", {"ID": uid}).get("result") or []
                if res:
                    name = full_name(res[0])
            except Fail:
                pass
        self._names[uid] = name
        return name


def load_config():
    path = secrets_path()
    if not os.path.exists(path):
        raise Fail(NOT_CONFIGURED)
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        parse_webhook(cfg["webhook"])
        int(cfg["user_id"])
    except (OSError, ValueError, KeyError, TypeError, Fail):
        raise Fail("Файл с ключом Битрикс24 повреждён: запусти настройку Битрикс24 заново")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return cfg


# ---------- разбор задач ----------

def g(obj, *names):
    for n in names:
        if isinstance(obj, dict) and obj.get(n) not in (None, ""):
            return obj.get(n)
    return None


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def task_id(t):
    v = g(t, "id", "ID")
    return to_int(v) or v


def status_of(t):
    return to_int(g(t, "status", "STATUS", "realStatus", "REAL_STATUS"))


def full_name(u):
    parts = [str(u.get(k) or "").strip() for k in ("NAME", "LAST_NAME")]
    name = " ".join(p for p in parts if p)
    return name or ("сотрудник %s" % u.get("ID"))


def tags_of(t):
    raw = g(t, "tags", "TAGS")
    if isinstance(raw, dict):
        raw = list(raw.values())
    names = []
    if isinstance(raw, list):
        for x in raw:
            if isinstance(x, dict):
                x = x.get("title") or x.get("name") or x.get("NAME") or x.get("TITLE")
            if x:
                names.append(str(x).strip())
    elif isinstance(raw, str) and raw.strip():
        names = [s.strip() for s in raw.split(",") if s.strip()]
    return names


def list_of(t):
    tags = tags_of(t)
    for tag in tags:
        if tag in AGENT_LISTS:
            return tag
    return tags[0] if tags else DEFAULT_LIST


def parse_dt(v):
    if not v:
        return None
    s = str(v).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                d = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if d is None:
        return None
    if d.tzinfo is not None:
        d = d.astimezone().replace(tzinfo=None)
    return d


def clean_notes(text):
    lines = [ln for ln in str(text or "").replace("\r\n", "\n").split("\n") if not ln.strip().startswith(KEY_PREFIX)]
    return "\n".join(lines).strip()[:500]


def describe(ctx, t, today):
    tid = task_id(t)
    item = {"id": tid, "title": str(g(t, "title", "TITLE") or "").strip(), "list": list_of(t)}
    d = parse_dt(g(t, "deadline", "DEADLINE"))
    if d:
        item["due"] = d.strftime("%Y-%m-%d")
        if (d.hour, d.minute) not in NO_TIME:
            item["time"] = d.strftime("%H:%M")
        days = (today - d.date()).days
        if days > 0:
            item["overdue_days"] = days
    notes = clean_notes(g(t, "description", "DESCRIPTION"))
    if notes:
        item["notes"] = notes
    if to_int(g(t, "priority", "PRIORITY")) == 2:
        item["priority"] = "высокий"
    item["url"] = ctx.url(tid)
    return item


def fetch_tasks(ctx, flt, order=None):
    tasks, start = [], 0
    while True:
        payload = ctx.bx.call("tasks.task.list", {"filter": flt, "select": SELECT,
                                                  "order": order or {"DEADLINE": "asc"}, "start": start})
        res = payload.get("result")
        page = res.get("tasks") if isinstance(res, dict) else res
        page = page if isinstance(page, list) else []
        tasks.extend(page)
        nxt = to_int(payload.get("next"))
        if not page or not nxt or nxt <= start or len(tasks) >= MAX_TASKS:
            break
        start = nxt
    return tasks[:MAX_TASKS]


def my_open(ctx):
    """Открытые задачи, где я ответственный (без «ждёт контроля»: свою часть я уже сделал)."""
    raw = fetch_tasks(ctx, {"RESPONSIBLE_ID": ctx.me, "<REAL_STATUS": 5})
    return [t for t in raw if to_int(g(t, "responsibleId", "RESPONSIBLE_ID")) == ctx.me
            and status_of(t) not in (4, 5, 7)]


def delegated_open(ctx):
    raw = fetch_tasks(ctx, {"CREATED_BY": ctx.me, "!RESPONSIBLE_ID": ctx.me, "<REAL_STATUS": 5})
    res = []
    for t in raw:
        rid = to_int(g(t, "responsibleId", "RESPONSIBLE_ID"))
        if to_int(g(t, "createdBy", "CREATED_BY")) == ctx.me and rid and rid != ctx.me \
                and status_of(t) not in (5, 7):
            res.append(t)
    return res


def responsible_name(ctx, t):
    resp = t.get("responsible")
    if isinstance(resp, dict) and resp.get("name"):
        return str(resp["name"]).strip()
    return ctx.user_name_by_id(to_int(g(t, "responsibleId", "RESPONSIBLE_ID")))


# ---------- сотрудники ----------

def norm(s):
    return " ".join(str(s or "").lower().replace("ё", "е").split())


def name_words(u):
    return [norm(u.get(k)) for k in ("NAME", "LAST_NAME", "SECOND_NAME") if norm(u.get(k))]


def is_active(u):
    return u.get("ACTIVE") in (None, True, "Y", "y", "true", "1", 1)


def search_users(ctx, query):
    tokens = norm(query).split()
    if not tokens:
        return []

    def matches(users):
        good = []
        for u in users if isinstance(users, list) else []:
            if not isinstance(u, dict) or not is_active(u):
                continue
            words = name_words(u)
            if all(any(w.startswith(tok) for w in words) for tok in tokens):
                good.append(u)
        return good

    found = matches(ctx.bx.call("user.search", {"FILTER": {"FIND": query.strip()}}).get("result"))
    if not found:
        longest = max(tokens, key=len)
        for field in ("LAST_NAME", "NAME"):
            found = matches(ctx.bx.call("user.search", {"FILTER": {field: longest.capitalize()}}).get("result"))
            if found:
                break
    seen, uniq = set(), []
    for u in found:
        if to_int(u.get("ID")) not in seen:
            seen.add(to_int(u.get("ID")))
            uniq.append(u)
    return uniq


def user_brief(u):
    item = {"id": to_int(u.get("ID")), "name": full_name(u)}
    pos = str(u.get("WORK_POSITION") or "").strip()
    if pos:
        item["position"] = pos
    return item


def resolve(ctx, assignee):
    """{"self": True} | {"user": {...}} | {"problem": "...", "candidates": [...]}"""
    a = (assignee or "").strip()
    if not a or norm(a) in SELF_WORDS or (ctx.user_name and norm(a) == norm(ctx.user_name)):
        return {"self": True}
    users = search_users(ctx, a)
    tokens = set(norm(a).split())
    exact = [u for u in users if tokens == set(name_words(u)) or tokens == set(name_words(u)[:2])]
    pick = users[0] if len(users) == 1 else (exact[0] if len(exact) == 1 else None)
    if pick is not None:
        if to_int(pick.get("ID")) == ctx.me:
            return {"self": True}
        return {"user": user_brief(pick)}
    if not users:
        return {"problem": "не нашёл в Битрикс24 сотрудника «%s»" % a, "candidates": []}
    cands = [user_brief(u) for u in users[:5]]
    names = ", ".join(c["name"] + (" (%s)" % c["position"] if c.get("position") else "") for c in cands)
    return {"problem": "под «%s» подходят несколько сотрудников: %s; уточни имя и фамилию" % (a, names),
            "candidates": cands}


# ---------- создание ----------

def build_deadline(due, tm):
    due = str(due or "").strip()
    if not due:
        return None
    try:
        day = datetime.strptime(due, "%Y-%m-%d")
    except ValueError:
        raise Fail("неверная дата «%s», нужен формат ГГГГ-ММ-ДД" % due[:20])
    tm = str(tm or "").strip()
    if tm:
        m = re.match(r"^(\d{1,2}):(\d{2})$", tm)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise Fail("неверное время «%s», нужен формат ЧЧ:ММ" % tm[:10])
        h, mi = int(m.group(1)), int(m.group(2))
    else:
        h, mi = END_OF_DAY
    return day.replace(hour=h, minute=mi).astimezone().isoformat(timespec="seconds")


def create_task(ctx, title, notes, key, lst, deadline, priority, responsible):
    fields = {"TITLE": title, "RESPONSIBLE_ID": responsible, "TAGS": [lst]}
    desc = str(notes or "").strip()
    if key:
        desc += ("\n" if desc else "") + KEY_PREFIX + key
    if desc:
        fields["DESCRIPTION"] = desc
    if deadline:
        fields["DEADLINE"] = deadline
    if str(priority or "").lower() == "high":
        fields["PRIORITY"] = 2
    res = ctx.bx.call("tasks.task.add", {"fields": fields}).get("result")
    task = res.get("task") if isinstance(res, dict) else None
    tid = task_id(task) if isinstance(task, dict) else (to_int(res) or None)
    if not tid:
        raise Fail("Битрикс24 не вернул номер новой задачи")
    return tid


def load_proposals():
    data = load_state("bitrix_proposals.json", {"next_n": 1, "items": []})
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        data = {"next_n": 1, "items": []}
    data["next_n"] = max([to_int(data.get("next_n")) or 1] + [to_int(p.get("n")) + 1 for p in data["items"]])
    return data


def parse_numbers(args, cmd):
    nums = []
    for a in args:
        for part in re.split(r"[,\s]+", a):
            part = part.strip().lstrip("#№")
            if part:
                if not part.isdigit():
                    raise Fail("Номера предложений должны быть числами: %s 1 2" % cmd)
                nums.append(int(part))
    if not nums:
        raise Fail("Укажи номера предложений: %s 1 2" % cmd)
    return nums


def proposal_view(p):
    v = {"n": p["n"], "title": p["title"], "assignee": p.get("user_name") or p.get("assignee") or ""}
    for k in ("due", "time", "list"):
        if p.get(k):
            v[k] = p[k]
    if p.get("problem"):
        v["problem"] = p["problem"]
    return v


# ---------- команды ----------

def ask_webhook():
    prompt = ("Вставь ссылку входящего вебхука Битрикс24 (Разработчикам, Другое, Входящий вебхук, "
              "права Задачи и Пользователи). Ссылка хранится только на этом компьютере.")
    if sys.platform == "darwin" and os.environ.get("BRIEF_NO_GUI") != "1":
        script = ['on run argv',
                  'text returned of (display dialog (item 1 of argv) default answer "" with hidden answer '
                  'with title "Битрикс24" buttons {"Отмена", "Сохранить"} default button "Сохранить" '
                  'cancel button "Отмена")',
                  'end run']
        cmd = ["osascript"]
        for line in script:
            cmd += ["-e", line]
        try:
            r = subprocess.run(cmd + [prompt], capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.SubprocessError):
            raise Fail("Не удалось показать окно для ввода ссылки вебхука")
        if r.returncode != 0:
            if "-128" in (r.stderr or ""):
                raise Fail("Настройка Битрикс24 отменена")
            raise Fail("Не удалось показать окно для ввода ссылки вебхука")
        return r.stdout.strip()
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass(prompt + "\nСсылка (символы не видны): ")
    sys.stderr.write(prompt + "\n")
    return (sys.stdin.readline() if sys.stdin is not None else "").strip()


def cmd_setup(args):
    p = parse_webhook(ask_webhook())
    bx = Bitrix(p["webhook"])
    prof = bx.call("profile").get("result") or {}
    me = to_int(prof.get("ID")) or p["user_id"]
    bx.call("tasks.task.list", {"filter": {"RESPONSIBLE_ID": me}, "select": ["ID"],
                                "order": {"ID": "desc"}, "start": 0})
    users = bx.call("user.get", {"ID": me}).get("result") or []
    name = full_name(users[0]) if users and isinstance(users[0], dict) else full_name(prof)
    write_private(secrets_path(), {"webhook": p["webhook"], "user_id": me, "user_name": name, "portal": p["portal"]})
    out({"ok": True, "user": name, "portal": p["portal"]})


def cmd_list(args):
    days = parse_days(args)
    ctx = Ctx(load_config())
    today = date.today()
    horizon = (today + timedelta(days=days)).strftime("%Y-%m-%d")
    dated, undated, counts = [], [], {}
    for t in my_open(ctx):
        d = describe(ctx, t, today)
        counts[d["list"]] = counts.get(d["list"], 0) + 1
        if "due" in d:
            if d["due"] <= horizon:
                dated.append(d)
        elif any(tag in AGENT_LISTS for tag in tags_of(t)):
            undated.append(d)
    dated.sort(key=lambda x: (x["due"], x.get("time", "99:99")))
    delegated = []
    for t in delegated_open(ctx):
        d = describe(ctx, t, today)
        st = status_of(t)
        item = {"id": d["id"], "title": d["title"], "responsible": responsible_name(ctx, t),
                "status": "сделана, ждёт твоей проверки" if st == 4 else STATUS_TEXT.get(st, "")}
        for k in ("due", "time", "overdue_days"):
            if k in d:
                item[k] = d[k]
        item["url"] = d["url"]
        delegated.append(item)
    delegated.sort(key=lambda x: (-x.get("overdue_days", 0), x.get("due", "9999")))
    out({"today": today.strftime("%Y-%m-%d"), "due": dated, "agent_lists_undated": undated,
         "open_by_list": counts, "delegated": delegated})


def cmd_done(args):
    days = parse_days(args)
    ctx = Ctx(load_config())
    today = date.today()
    since = today - timedelta(days=days)
    since_iso = datetime(since.year, since.month, since.day).astimezone().isoformat(timespec="seconds")
    res = []
    for t in fetch_tasks(ctx, {"RESPONSIBLE_ID": ctx.me, ">=CLOSED_DATE": since_iso}, {"CLOSED_DATE": "asc"}):
        closed = parse_dt(g(t, "closedDate", "CLOSED_DATE"))
        if not closed or closed.date() < since or to_int(g(t, "responsibleId", "RESPONSIBLE_ID")) != ctx.me:
            continue
        if status_of(t) not in (0, 4, 5):
            continue
        d = describe(ctx, t, today)
        d.pop("overdue_days", None)
        d["completed"] = closed.strftime("%Y-%m-%d")
        res.append(d)
    res.sort(key=lambda x: x["completed"])
    out({"from": since.strftime("%Y-%m-%d"), "completed": res})


def cmd_add(args):
    items = read_input(args, "add <файл.json>")
    ctx = Ctx(load_config())
    keys = load_state("bitrix_keys.json", {})
    props = load_proposals()
    pending_keys = set(p.get("key") for p in props["items"] if p.get("key"))
    created, skipped, refused, proposed = [], [], [], []
    for item in items[:MAX_ADD]:
        title = " ".join(str(item.get("title") or "").split())
        lst = str(item.get("list") or AGENT_LISTS[0]).strip()
        key = str(item.get("key") or "").strip()
        if not title:
            refused.append("%s: пустой заголовок" % lst)
            continue
        if lst == BRIEF_LIST:
            refused.append("%s: уведомления «%s» в Битрикс24 не ставятся" % (title, BRIEF_LIST))
            continue
        if key and (key in keys or key in pending_keys):
            skipped.append(title)
            continue
        try:
            deadline = build_deadline(item.get("due"), item.get("time"))
            who = resolve(ctx, item.get("assignee"))
            if who.get("self"):
                tid = create_task(ctx, title, item.get("notes"), key, lst, deadline, item.get("priority"), ctx.me)
                if key:
                    keys[key] = tid
                    write_private(state_path("bitrix_keys.json"), keys)
                created.append({"id": tid, "title": title, "url": ctx.url(tid)})
                continue
        except Fail as e:
            refused.append("%s: %s" % (title, e))
            continue
        user = who.get("user") or {}
        p = {"n": props["next_n"], "title": title, "notes": str(item.get("notes") or ""),
             "due": str(item.get("due") or ""), "time": str(item.get("time") or ""), "key": key, "list": lst,
             "priority": str(item.get("priority") or ""), "assignee": str(item.get("assignee") or "").strip(),
             "user_id": user.get("id"), "user_name": user.get("name"), "candidates": who.get("candidates", []),
             "problem": who.get("problem"), "created": datetime.now().astimezone().isoformat(timespec="seconds")}
        props["next_n"] += 1
        props["items"].append(p)
        if key:
            pending_keys.add(key)
        write_private(state_path("bitrix_proposals.json"), props)
        proposed.append(proposal_view(p))
    result = {"created": created, "skipped": skipped, "refused": refused, "proposed": proposed}
    if proposed:
        result["note"] = ("Задачи другим людям не поставлены. Покажи владельцу список с номерами; "
                          "ставить только после его «ставь N» (команда confirm N)")
    out(result)


def cmd_proposals(args):
    props = load_proposals()
    out({"proposals": [proposal_view(p) for p in props["items"]]})


def cmd_confirm(args):
    nums = parse_numbers(args, "confirm")
    ctx = Ctx(load_config())
    keys = load_state("bitrix_keys.json", {})
    props = load_proposals()
    created, skipped, refused, not_found = [], [], [], []
    for n in nums:
        p = next((x for x in props["items"] if to_int(x.get("n")) == n), None)
        if p is None:
            not_found.append(n)
            continue
        if not p.get("user_id"):
            refused.append({"n": n, "title": p["title"],
                            "reason": p.get("problem") or "не понятно, кому ставить задачу"})
            continue
        key = p.get("key") or ""
        if key and key in keys:
            skipped.append({"n": n, "title": p["title"]})
        else:
            try:
                deadline = build_deadline(p.get("due"), p.get("time"))
                tid = create_task(ctx, p["title"], p.get("notes"), key, p.get("list") or AGENT_LISTS[0],
                                  deadline, p.get("priority"), int(p["user_id"]))
            except Fail as e:
                refused.append({"n": n, "title": p["title"], "reason": str(e)})
                continue
            if key:
                keys[key] = tid
                write_private(state_path("bitrix_keys.json"), keys)
            created.append({"n": n, "id": tid, "title": p["title"], "responsible": p.get("user_name") or "",
                            "url": ctx.url(tid)})
        props["items"] = [x for x in props["items"] if x is not p]
        write_private(state_path("bitrix_proposals.json"), props)
    out({"created": created, "skipped": skipped, "refused": refused, "not_found": not_found})


def cmd_reject(args):
    nums = parse_numbers(args, "reject")
    props = load_proposals()
    have = set(to_int(p.get("n")) for p in props["items"])
    rejected = [n for n in nums if n in have]
    props["items"] = [p for p in props["items"] if to_int(p.get("n")) not in set(nums)]
    write_private(state_path("bitrix_proposals.json"), props)
    out({"rejected": rejected, "not_found": [n for n in nums if n not in have]})


def cmd_complete(args):
    items = read_input(args, "complete <файл.json>")
    ctx = Ctx(load_config())
    pool, seen = [], set()
    for t in fetch_tasks(ctx, {"RESPONSIBLE_ID": ctx.me, "<REAL_STATUS": 5}) + \
            fetch_tasks(ctx, {"CREATED_BY": ctx.me, "<REAL_STATUS": 5}):
        tid = task_id(t)
        resp = to_int(g(t, "responsibleId", "RESPONSIBLE_ID"))
        creator = to_int(g(t, "createdBy", "CREATED_BY"))
        st = status_of(t)
        if tid in seen or ctx.me not in (resp, creator) or st in (5, 7) or (st == 4 and creator != ctx.me):
            continue
        seen.add(tid)
        pool.append(t)
    completed, not_found, ambiguous = [], [], []
    for item in items[:MAX_COMPLETE]:
        title = " ".join(str(item.get("title") or "").split())
        want_id = to_int(item.get("id"))
        lst = str(item.get("list") or "").strip()
        if want_id:
            hits = [t for t in pool if to_int(task_id(t)) == want_id]
        else:
            hits = [t for t in pool if norm(g(t, "title", "TITLE")) == norm(title) and title
                    and (not lst or lst in tags_of(t) or lst == list_of(t))]
        if not hits:
            not_found.append(title or str(item.get("id") or ""))
            continue
        if len(hits) > 1:
            ambiguous.append({"title": title, "ids": [task_id(t) for t in hits],
                              "lists": [list_of(t) for t in hits]})
            continue
        t = hits[0]
        try:
            ctx.bx.call("tasks.task.complete", {"taskId": task_id(t)})
        except Fail as e:
            not_found.append("%s: %s" % (title or task_id(t), e))
            continue
        completed.append({"id": task_id(t), "title": str(g(t, "title", "TITLE") or ""), "list": list_of(t)})
        pool.remove(t)
    out({"completed": completed, "not_found": not_found, "ambiguous": ambiguous})


def cmd_users(args):
    query = " ".join(args).strip()
    if not query:
        raise Fail("Укажи, кого искать: users <имя или фамилия>")
    ctx = Ctx(load_config())
    out({"users": [user_brief(u) for u in search_users(ctx, query)[:20]]})


def cmd_doctor(args):
    try:
        cfg = load_config()
    except Fail as e:
        out({"ok": False, "configured": False, "error": str(e)}, 1, compact=True)
    ctx = Ctx(cfg)
    base = {"configured": True, "user": cfg.get("user_name") or "", "portal": ctx.portal}
    try:
        ctx.bx.call("profile")
        ctx.bx.call("tasks.task.list", {"filter": {"RESPONSIBLE_ID": ctx.me}, "select": ["ID"], "start": 0})
    except Fail as e:
        base.update({"ok": False, "webhook_works": False, "error": str(e)})
        out(base, 1, compact=True)
    base.update({"ok": True, "webhook_works": True})
    out(base, compact=True)


COMMANDS = {"setup": cmd_setup, "list": cmd_list, "done": cmd_done, "add": cmd_add, "proposals": cmd_proposals,
            "confirm": cmd_confirm, "reject": cmd_reject, "complete": cmd_complete, "users": cmd_users,
            "doctor": cmd_doctor}
USAGE = ("Команды: setup | list [дней] | done [дней] | add <файл> | proposals | confirm <N> | reject <N> | "
         "complete <файл> | users <запрос> | doctor")


def main(argv):
    try:
        if not argv or argv[0] not in COMMANDS:
            raise Fail(USAGE)
        COMMANDS[argv[0]](argv[1:])
    except Fail as e:
        out({"error": str(e)}, 1)
    except KeyboardInterrupt:
        out({"error": "Прервано"}, 1)
    except Exception as e:  # noqa: BLE001
        out({"error": "Сбой коннектора Битрикс24: %s: %s" % (type(e).__name__, str(e)[:200])}, 1)


if __name__ == "__main__":
    main(sys.argv[1:])
