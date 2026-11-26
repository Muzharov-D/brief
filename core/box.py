# -*- coding: utf-8 -*-
"""Мини-приложение Огонька в Telegram на живых данных: зашифрованный ящик.

Mac раз в несколько минут собирает день (встречи, задачи, утренний брифинг, протоколы), шифрует его
своим ключом и кладёт в ящик на сервере мини-приложения. Ключ знают только этот Mac и кнопка вашего бота
(в адресе после #, на сервер такая часть адреса не уходит). Сервер хранит шифротекст и прочитать его не может.
К самому Mac никто не подключается, мини-приложение работает и когда Mac спит (показывает последний снимок).
Обратно мини-приложение кладёт зашифрованные действия («задача готова»), Mac забирает и выполняет их сам.

  brief app setup      завести ключи, поставить кнопку «Огонёк» в вашего бота, загрузить день
  brief app sync       собрать день, загрузить, выполнить действия из мини-приложения
  brief app install    обновлять каждые 5 минут (launchd); brief app remove  убрать
  brief app status     когда загружено последний раз
"""
import base64
import hashlib
import json
import os
import plistlib
import re
import secrets
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import brief as B

CONF = B.DATA / "miniapp.json"
LAST = B.STATE / "miniapp-last.json"
# Адрес на своём домене (прокси Caddy на avandoc.online к тому же Vercel): мини-приложения на *.vercel.app у свежих
# ботов Telegram принимает за фишинг. Старый адрес остаётся запасным, пока новый не отвечает, а ящики уже
# настроенных компьютеров переезжают сами при следующей сборке дня (данные те же, хранилище одно).
NEW_HOST = "https://avandoc.online/ogonyok"
OLD_HOST = "https://ogonyok-demo.vercel.app"
HOST_ENV = os.environ.get("BRIEF_MINIAPP_HOST")
LABEL = "local.brief.miniapp"
EVERY = 300
CALL_HOSTS = (("zoom.us", "Zoom"), ("telemost", "Телемост"), ("ktalk", "Контур Толк"), ("teams.microsoft", "Teams"),
              ("meet.google", "Google Meet"), ("jazz.sber", "Jazz"), ("salutejazz", "Jazz"), ("mts-link", "МТС Линк"),
              ("webinar.ru", "МТС Линк"), ("dion.vc", "Dion"), ("vk.com/call", "VK Звонки"), ("webex", "Webex"))
THEIRS = "Жду от других"


def b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def conf():
    try:
        return json.loads(CONF.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def reachable(host):
    """Отвечает ли мини-приложение по адресу: пустой ящик даёт 404 от нашего же сервера."""
    try:
        with urllib.request.urlopen(f"{host}/api/box?id={'0' * 32}&part=day", timeout=10) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return e.code == 404 and "json" in (e.headers.get("Content-Type") or "")
    except OSError:
        return False


def best_host():
    return HOST_ENV or (NEW_HOST if reachable(NEW_HOST) else OLD_HOST)


def move_host(c):
    """Ящик на старом адресе, а новый уже работает: переносим адрес и кнопку бота. Данные те же (одно хранилище)."""
    if HOST_ENV or c.get("host") != OLD_HOST or not reachable(NEW_HOST):
        return c
    c["host"] = NEW_HOST
    CONF.write_text(json.dumps(c, indent=2), encoding="utf-8")
    CONF.chmod(0o600)
    set_button(c)
    return c


def new_conf():
    write = secrets.token_hex(32)
    c = {"box": hashlib.sha256(write.encode()).hexdigest()[:32], "write": write, "key": b64(secrets.token_bytes(32)),
         "host": best_host(), "created": datetime.now().isoformat(timespec="seconds")}
    CONF.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    CONF.write_text(json.dumps(c, indent=2), encoding="utf-8")
    CONF.chmod(0o600)
    return c


def app_url(c):
    return f"{c['host']}/#box={c['box']}&k={c['key']}"


def aes(c):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(base64.urlsafe_b64decode(c["key"] + "=" * (-len(c["key"]) % 4)))


def seal(c, obj):
    iv = secrets.token_bytes(12)
    ct = aes(c).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode(), None)
    return {"iv": base64.b64encode(iv).decode(), "ct": base64.b64encode(ct).decode()}


def unseal(c, o):
    return json.loads(aes(c).decrypt(base64.b64decode(o["iv"]), base64.b64decode(o["ct"]), None))


def http(method, url, body=None, key=None):
    req = urllib.request.Request(url, method=method, data=body.encode() if body is not None else None,
                                 headers={"Content-Type": "text/plain", **({"X-Box-Key": key} if key else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


# ---------- день из коннекторов ----------

def get(cap, *args, p=None):
    if cap not in ((p or {}).get("connectors") or {}):
        return None
    code, out = B.cmd_run([cap, *args], capture=True, timeout=300)
    try:
        d = json.loads(out)
    except ValueError:
        return None
    return None if code or (isinstance(d, dict) and d.get("error")) else d


def short(s, n=400):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def call_host(links):
    for l in links or []:
        for h, name in CALL_HOSTS:
            if h in l:
                return name
    return ""


def offset(d, today):
    return (d - today).days


def meetings(p, now):
    today = now.date()
    back = now.hour + 1
    ahead = int((datetime.combine(today + timedelta(days=2), datetime.min.time()) - now).total_seconds() // 3600) + 1
    cal = get("calendar", str(ahead), str(back), p=p) or {}
    me = (p.get("name") or "").lower()
    out = []
    for e in cal.get("events", []):
        if e.get("canceled") or e.get("all_day"):
            continue
        try:
            st = datetime.fromisoformat(str(e["start"]).replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
            en = datetime.fromisoformat(str(e["end"]).replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
        except (KeyError, ValueError):
            continue
        if st.date() < today:
            continue
        people = []
        for a in e.get("attendees") or []:
            name = re.sub(r"\s*\(.*?\)\s*$", "", a).strip()
            if name and name != e.get("me") and (not me or me not in name.lower()) and name not in people:
                people.append(name)
        loc = (e.get("location") or "").strip()
        place = "" if (not loc or loc.startswith("http")) else short(loc, 80)
        mid = hashlib.sha1(f"{e.get('title')}{e['start']}".encode()).hexdigest()[:10]
        out.append({"id": "m" + mid, "day": offset(st.date(), today), "start": f"{st:%H:%M}", "end": f"{en:%H:%M}",
                    "title": e.get("title") or "Без названия", "people": people[:12] or ["Без участников"],
                    "link": call_host(e.get("links")), "place": place, "my_status": e.get("my_status") or "",
                    "status": "later", "_start": st})
    out.sort(key=lambda m: m["_start"])
    for m in out:
        m.pop("_start")
    return out


def tasks(p, today):
    d = get("tasks", "list", "7", p=p) or {}
    seen, res = set(), []
    lists = lambda k: [t for t in (d.get(k) if isinstance(d.get(k), list) else []) if isinstance(t, dict)]
    rows = lists("today") + lists("due") + [dict(t, due="") for t in lists("agent_lists_undated")]
    for t in rows:
        title, lst = (t.get("title") or "").strip(), t.get("list") or ""
        if not title or (title, lst) in seen or lst == "Брифинг":
            continue
        seen.add((title, lst))
        due, time_ = None, ""
        m = re.match(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}))?", str(t.get("due") or ""))
        if m:
            due = offset(datetime.strptime(m.group(1), "%Y-%m-%d").date(), today)
            time_ = m.group(2) or ""
        mine = lst != THEIRS
        who = ""
        if not mine:
            who, _, rest = title.partition(":")
            if not rest:
                who = ""
        note = (t.get("notes") or "").splitlines()
        src = next((x.split(":", 1)[0].strip().lower() for x in note if x.startswith(("Созвон", "Письмо", "Чат", "Голосовое"))), "")
        res.append({"id": "t" + hashlib.sha1(f"{title}|{lst}".encode()).hexdigest()[:10], "text": title, "list": lst,
                    "due": due, "time": time_, "mine": mine, "who": who.strip() if who else "",
                    "from": {"созвон": "из созвона", "письмо": "из письма", "чат": "из переписки",
                             "голосовое": "из голосового"}.get(src, ""),
                    "late": int(t.get("overdue_days") or 0)})
    return res


def latest(folder, today, evening=False):
    ws = B.workspace()
    f = ws / folder
    if not f.exists():
        return None
    name = today.isoformat() + ("-вечер" if evening else "") + ".md"
    x = f / name
    return x if x.exists() else None


def sections(md):
    """Брифинг: заголовки ## и их текст."""
    out, cur = [], None
    for line in md.splitlines():
        if line.startswith("## "):
            cur = {"h": line[3:].strip(), "lines": []}
            out.append(cur)
        elif cur is not None and line.strip():
            cur["lines"].append(line.rstrip())
    return [{"h": s["h"], "text": "\n".join(s["lines"])[:2500]} for s in out]


def protocols(today, days=3):
    f = B.workspace() / "Созвоны"
    res = []
    if not f.exists():
        return res
    for x in sorted(f.glob("*.md"), key=lambda y: y.name, reverse=True):
        m = re.match(r"(\d{4}-\d{2}-\d{2}) (\d{2})-(\d{2}) (.+)\.md$", x.name)
        if not m:
            continue
        d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        if (today - d).days > days:
            break
        title = re.sub(r"\s*\(.*\)$", "", m.group(4)).strip()
        res.append({"id": "p" + hashlib.sha1(x.name.encode()).hexdigest()[:10], "day": offset(d, today),
                    "start": f"{m.group(2)}:{m.group(3)}", "title": title, "status": "done",
                    "people": [s.strip() for s in (re.search(r"\((.+)\)$", m.group(4)) or [None, ""])[1].split(",") if s.strip()],
                    "protocol_md": sections(x.read_text(encoding="utf-8", errors="replace"))})
        if len(res) >= 5:
            break
    return res


def build_day(p=None):
    p = B.load_profile() if p is None else p
    now = datetime.now()
    today = now.date()
    morning = latest("Брифинги", today)
    evening = latest("Брифинги", today, evening=True)
    day = {"live": True, "owner": {"name": p.get("name") or ""}, "assistant": p.get("assistant_name") or "Огонёк",
           "bot": bot_username(),
           "built": now.isoformat(timespec="minutes"),
           "brief": {"date": today.isoformat(), "waiting": [], "time": "", "sections": []},
           "meetings": meetings(p, now), "tasks": tasks(p, today), "ask": [], "protocols": protocols(today)}
    for f, kind in ((morning, "morning"), (evening, "evening")):
        if f:
            day["brief"].setdefault("files", []).append(kind)
    src = evening or morning
    if src:
        day["brief"]["time"] = datetime.fromtimestamp(src.stat().st_mtime).strftime("%H:%M")
        day["brief"]["title"] = "Вечерний разбор" if src is evening else "Утренний брифинг"
        day["brief"]["sections"] = sections(src.read_text(encoding="utf-8", errors="replace"))
    return day


# ---------- бот ----------

def bot():
    f = B.DATA / "telegram" / "bot.json"
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


def bot_username():
    b = bot() or {}
    return b.get("username") or ""


def set_button(c):
    b = bot()
    if not b:
        return "бот не подключён: сначала brief setup, шаг Telegram"
    payload = {"chat_id": b["chat_id"], "menu_button": {"type": "web_app", "text": "Огонёк", "web_app": {"url": app_url(c)}}}
    req = urllib.request.Request(f"https://api.telegram.org/bot{b['token']}/setChatMenuButton",
                                 data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = json.loads(r.read()).get("ok")
    except OSError as e:
        return f"Telegram недоступен: {e}"
    return "" if ok else "Telegram не принял кнопку"


# ---------- действия из мини-приложения ----------

def apply_actions(c, day):
    try:
        raw = json.loads(http("GET", f"{c['host']}/api/box?id={c['box']}&part=act", key=c["write"]))
    except (OSError, ValueError):
        return []
    done, by_id = [], {t["id"]: t for t in day.get("tasks", [])}
    items = []
    for o in raw:
        try:
            a = unseal(c, o)
        except Exception:  # noqa: BLE001: чужое или битое действие пропускаем
            continue
        t = by_id.get(a.get("task"))
        if a.get("do") == "done" and t:
            items.append({"title": t["text"], "list": t["list"]})
    if items:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump({"items": items[:10]}, f, ensure_ascii=False)
        try:
            os.environ.setdefault("BRIEF_ACTOR", "мини-приложение")
            B.cmd_run(["tasks", "complete", f.name], capture=True, timeout=120)
            done = [i["title"] for i in items]
        finally:
            os.unlink(f.name)
    return done


def sync(quiet=False):
    c = conf()
    if not c:
        print(json.dumps({"error": "мини-приложение не настроено: brief app setup"}, ensure_ascii=False))
        return 1
    c = move_host(c)
    day = build_day()
    done = apply_actions(c, day)
    if done:  # задачи закрыты: собрать день заново, чтобы мини-приложение увидело их выполненными
        day = build_day()
    try:
        http("PUT", f"{c['host']}/api/box?id={c['box']}&part=day", json.dumps(seal(c, day)), key=c["write"])
        ok = True
    except OSError as e:
        ok, err = False, str(e)
    info = {"ok": ok, "at": datetime.now().isoformat(timespec="seconds"), "meetings": len(day["meetings"]),
            "tasks": len(day["tasks"]), "done_from_app": done, **({} if ok else {"error": err})}
    B.STATE.mkdir(parents=True, exist_ok=True)
    LAST.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    if not quiet:
        print(json.dumps(info, ensure_ascii=False))
    return 0 if ok else 1


def setup():
    c = conf() or new_conf()
    err = set_button(c)
    code = sync(quiet=True)
    last = json.loads(LAST.read_text(encoding="utf-8")) if LAST.exists() else {}
    print(json.dumps({"button": not err, **({"button_error": err} if err else {}), "uploaded": code == 0,
                      "meetings": last.get("meetings"), "tasks": last.get("tasks"),
                      "next": "Откройте своего бота в Telegram: слева от поля ввода кнопка «Огонёк»."}, ensure_ascii=False))


def install(remove=False):
    uid = os.getuid()
    plist = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    plist.unlink(missing_ok=True)
    if remove:
        print(json.dumps({"removed": True}, ensure_ascii=False))
        return
    app = B.HOME / "helpers" / "Огонёк.app" / "Contents" / "MacOS"
    exe = next((str(x) for x in sorted(app.glob("*")) if os.access(x, os.X_OK)), None) if app.exists() else None
    argv = ([exe] if exe else B.brief_argv()) + ["app", "sync", "quiet"]
    log = B.workspace() / "Журнал" / "miniapp.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    data = {"Label": LABEL, "ProgramArguments": argv, "StartInterval": EVERY, "RunAtLoad": True,
            "StandardOutPath": str(log), "StandardErrorPath": str(log), "ProcessType": "Background"}
    if exe:
        data["AssociatedBundleIdentifiers"] = ["local.brief.app"]
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps(data))
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], capture_output=True, text=True)
    print(json.dumps({"installed": r.returncode == 0, "every_min": EVERY // 60,
                      **({"error": r.stderr.strip()} if r.returncode else {})}, ensure_ascii=False))


def main(args):
    cmd = args[0] if args else "status"
    if cmd == "setup":
        setup()
    elif cmd == "sync":
        sys.exit(sync(quiet="quiet" in args))
    elif cmd == "install":
        install()
    elif cmd == "remove":
        install(remove=True)
    elif cmd == "day":  # для проверки: что уйдёт в мини-приложение (на этом компьютере, без отправки)
        print(json.dumps(build_day(), ensure_ascii=False, indent=2))
    else:
        c = conf()
        last = json.loads(LAST.read_text(encoding="utf-8")) if LAST.exists() else None
        print(json.dumps({"configured": bool(c), "last": last}, ensure_ascii=False))
