"""Классический Outlook для брифинга (Windows, COM). Только чтение, кроме трёх вещей:
черновиков писем (сохраняются в «Черновики», не отправляются), новых задач в списках агента
и создания этих списков. Письма не отмечаются прочитанными, ничего не отправляется и не удаляется.

  run outlook mail <часов> [символов=600] [inbox|sent] [regex]
        письма за N часов из всех ящиков: account, received, from, from_email, to, cc, subject,
        read, flagged, replied, body (начало текста). regex фильтрует по отправителю и теме
  run outlook calendar <часов_вперёд> [часов_назад=0]
        встречи: start, end, title, organizer, attendees, my_status, canceled, all_day, location, links, notes
  run outlook tasks list [дней=7]      открытые задачи со сроком до +N дней, просроченные, без срока из списков агента
  run outlook tasks done [дней=7]      выполненные за N дней
  run outlook tasks add <вход.json>    {"items":[{"list","title","notes","due":"ГГГГ-ММ-ДД","time":"ЧЧ:ММ","key","priority":"high"}]}
                                       только в списки ALLOWED_LISTS, только добавить; повтор key пропускается
  run outlook tasks complete <вход.json> {"items":[{"title","list"?}]} закрыть по точному заголовку
                                       (только когда она сама написала боту, что сделала)
  run outlook draft <вход.json>        {"items":[{"to":[],"cc":[],"subject","body"}]}, «Черновики», не больше 5
  run outlook doctor                   проверка доступа

Списки задач = подпапки папки «Задачи» Outlook; в Microsoft To Do на телефоне они видны как списки.
"""
import json
import locale
import re
import sys
from datetime import datetime, timedelta

import pythoncom  # noqa: F401  (нужен pywin32)
import win32com.client

ALLOWED_LISTS = ["Из созвонов", "Из переписки", "Жду от других"]
KEY_PREFIX = "key: "
MAX_DRAFTS = 5
NO_DATE_YEAR = 4000  # Outlook хранит «без срока» как 01.01.4501

OL_INBOX, OL_SENT, OL_CALENDAR, OL_TASKS, OL_DRAFTS = 6, 5, 9, 13, 16
OL_MAIL, OL_APPOINTMENT_CLASS, OL_TASK_CLASS = 43, 26, 48
PR_LAST_VERB = "http://schemas.microsoft.com/mapi/proptag/0x10810003"
PR_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
RESPONSE = {0: "нет", 1: "организатор", 2: "под вопросом", 3: "принято", 4: "отклонено", 5: "не ответил"}
LINK_RE = re.compile(r"https?://[^\s<>\"']+")
MEETING_HOSTS = ("teams.microsoft", "zoom.us", "telemost", "meet.google", "webex", "ktalk", "jazz.sber",
                 "dion.vc", "vk.com/call", "salutejazz", "bbb", "mts-link", "webinar.ru")


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def outlook():
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        return app, app.GetNamespace("MAPI")
    except Exception as e:  # noqa: BLE001
        out({"error": f"Outlook недоступен: {e}. Нужен классический Outlook, запущенный под тем же пользователем"})
        sys.exit(1)


def naive(dt):
    """pywin32 отдаёт местное время с пометкой UTC: убираем пометку, считаем местным."""
    if dt is None:
        return None
    return datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def stores(ns):
    for i in range(1, ns.Stores.Count + 1):
        s = ns.Stores.Item(i)
        yield s


def default_folder(store, kind):
    try:
        return store.GetDefaultFolder(kind)
    except Exception:  # noqa: BLE001: у архивов и общих ящиков папок по умолчанию нет
        return None


def sender_email(m):
    try:
        if m.SenderEmailType == "EX":
            u = m.Sender.GetExchangeUser()
            if u:
                return u.PrimarySmtpAddress
        return m.SenderEmailAddress
    except Exception:  # noqa: BLE001
        return ""


def recipients(m, kind):
    """kind 1 = Кому, 2 = Копия. Адреса SMTP, где удаётся."""
    res = []
    try:
        for i in range(1, m.Recipients.Count + 1):
            r = m.Recipients.Item(i)
            if r.Type != kind:
                continue
            addr = ""
            try:
                addr = r.PropertyAccessor.GetProperty(PR_SMTP)
            except Exception:  # noqa: BLE001
                addr = r.Address if "@" in (r.Address or "") else ""
            res.append(f"{r.Name} <{addr}>" if addr else r.Name)
    except Exception:  # noqa: BLE001
        pass
    return res


def mail(hours, chars=600, box="inbox", pattern=None):
    _, ns = outlook()
    since = datetime.now() - timedelta(hours=hours)
    rx = re.compile(pattern, re.I) if pattern else None
    kind = OL_SENT if box == "sent" else OL_INBOX
    res = []
    for s in stores(ns):
        folder = default_folder(s, kind)
        if folder is None:
            continue
        items = folder.Items
        field = "[SentOn]" if box == "sent" else "[ReceivedTime]"
        items.Sort(field, True)
        m = items.GetFirst()
        while m is not None:
            try:
                if m.Class != OL_MAIL:
                    m = items.GetNext()
                    continue
                when = naive(m.SentOn if box == "sent" else m.ReceivedTime)
                if when < since:
                    break
                frm = f"{m.SenderName} <{sender_email(m)}>"
                if rx and not rx.search(frm + " " + (m.Subject or "")):
                    m = items.GetNext()
                    continue
                try:
                    verb = m.PropertyAccessor.GetProperty(PR_LAST_VERB)
                except Exception:  # noqa: BLE001
                    verb = 0
                body = re.sub(r"\s+", " ", m.Body or "").strip()[:chars]
                res.append({"account": s.DisplayName, "received": fmt(when), "from": frm,
                            "to": recipients(m, 1), "cc": recipients(m, 2), "subject": m.Subject or "",
                            "read": not m.UnRead, "flagged": m.FlagStatus == 2,
                            "replied": verb in (102, 103), "forwarded": verb == 104, "body": body})
            except Exception as e:  # noqa: BLE001: одно битое письмо не роняет остальные
                res.append({"error": str(e)[:200]})
            m = items.GetNext()
    res.sort(key=lambda x: x.get("received", ""), reverse=True)
    out(res)


def restrict_range(items, start, end):
    """Restrict понимает дату только в формате региональных настроек Windows; пробуем его, потом US."""
    locale.setlocale(locale.LC_TIME, "")
    attempts = [("%x %H:%M", "%x %H:%M"), ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M %p")]
    for f1, f2 in attempts:
        q = f"[Start] < '{end.strftime(f2)}' AND [End] > '{start.strftime(f1)}'"
        try:
            return items.Restrict(q)
        except Exception:  # noqa: BLE001
            continue
    return None


def calendar(ahead, back=0):
    _, ns = outlook()
    now = datetime.now()
    start, end = now - timedelta(hours=back), now + timedelta(hours=ahead)
    events = []
    for s in stores(ns):
        folder = default_folder(s, OL_CALENDAR)
        if folder is None:
            continue
        items = folder.Items
        items.IncludeRecurrences = True
        items.Sort("[Start]")
        found = restrict_range(items, start, end)
        if found is None:
            events.append({"error": f"{s.DisplayName}: календарь не отфильтровался по датам"})
            continue
        a = found.GetFirst()
        while a is not None:
            try:
                st, en = naive(a.Start), naive(a.End)
                if en > start and st < end:  # повторная проверка: Restrict зависит от локали
                    body = a.Body or ""
                    links = [u for u in LINK_RE.findall((a.Location or "") + " " + body)
                             if any(h in u for h in MEETING_HOSTS)]
                    attendees = []
                    for i in range(1, a.Recipients.Count + 1):
                        r = a.Recipients.Item(i)
                        attendees.append({"name": r.Name, "response": RESPONSE.get(r.MeetingResponseStatus, "")})
                    events.append({
                        "account": s.DisplayName, "calendar": folder.Name, "title": a.Subject or "",
                        "start": fmt(st), "end": fmt(en), "all_day": bool(a.AllDayEvent),
                        "organizer": a.Organizer or "", "attendees": attendees,
                        "my_status": RESPONSE.get(a.ResponseStatus, ""),
                        "canceled": a.MeetingStatus in (5, 7) or (a.Subject or "").lower().startswith(("canceled", "отменено")),
                        "location": a.Location or "", "links": list(dict.fromkeys(links))[:3],
                        "notes": re.sub(r"\s+", " ", body).strip()[:600]})
            except Exception as e:  # noqa: BLE001
                events.append({"error": str(e)[:200]})
            a = found.GetNext()
    events.sort(key=lambda e: e.get("start", ""))
    out({"from": fmt(start), "to": fmt(end), "events": events})


def task_folders(ns):
    root = ns.GetDefaultFolder(OL_TASKS)
    folders = [root]
    for i in range(1, root.Folders.Count + 1):
        folders.append(root.Folders.Item(i))
    return root, folders


def describe(t, list_name, today):
    item = {"list": list_name, "title": t.Subject or ""}
    due = naive(t.DueDate)
    if due and due.year < NO_DATE_YEAR:
        item["due"] = due.strftime("%Y-%m-%d")
        days = (today - due.date()).days
        if days > 0 and not t.Complete:
            item["overdue_days"] = days
    if t.ReminderSet:
        rt = naive(t.ReminderTime)
        if rt and rt.year < NO_DATE_YEAR:
            item["time"] = rt.strftime("%H:%M")
    notes = "\n".join(line for line in (t.Body or "").splitlines() if not line.startswith(KEY_PREFIX)).strip()
    if notes:
        item["notes"] = notes[:500]
    if t.Importance == 2:
        item["priority"] = "высокий"
    return item


def tasks_list(days):
    _, ns = outlook()
    today = datetime.now().date()
    horizon = today + timedelta(days=days)
    _, folders = task_folders(ns)
    dated, undated, counts = [], [], {}
    for f in folders:
        items = f.Items
        t = items.GetFirst()
        while t is not None:
            try:
                if t.Class == OL_TASK_CLASS and not t.Complete:
                    counts[f.Name] = counts.get(f.Name, 0) + 1
                    d = describe(t, f.Name, today)
                    if "due" in d:
                        if d["due"] <= horizon.strftime("%Y-%m-%d"):
                            dated.append(d)
                    elif f.Name in ALLOWED_LISTS:
                        undated.append(d)
            except Exception:  # noqa: BLE001
                pass
            t = items.GetNext()
    dated.sort(key=lambda x: x["due"])
    out({"today": today.strftime("%Y-%m-%d"), "due": dated, "agent_lists_undated": undated, "open_by_list": counts})


def tasks_done(days):
    _, ns = outlook()
    today = datetime.now().date()
    since = today - timedelta(days=days)
    _, folders = task_folders(ns)
    res = []
    for f in folders:
        items = f.Items
        t = items.GetFirst()
        while t is not None:
            try:
                if t.Class == OL_TASK_CLASS and t.Complete:
                    done = naive(t.DateCompleted)
                    if done and done.year < NO_DATE_YEAR and done.date() >= since:
                        d = describe(t, f.Name, today)
                        d["completed"] = done.strftime("%Y-%m-%d")
                        res.append(d)
            except Exception:  # noqa: BLE001
                pass
            t = items.GetNext()
    res.sort(key=lambda x: x["completed"])
    out({"from": since.strftime("%Y-%m-%d"), "completed": res})


def tasks_add(path):
    _, ns = outlook()
    data = json.loads(open(path, encoding="utf-8").read())
    root, folders = task_folders(ns)
    by_name = {f.Name: f for f in folders[1:]}
    created, skipped, refused = [], [], []
    for item in data.get("items", []):
        title = (item.get("title") or "").strip()
        list_name = item.get("list") or ALLOWED_LISTS[0]
        if not title or list_name not in ALLOWED_LISTS:
            refused.append(f"{list_name}: {title}")
            continue
        try:
            folder = by_name.get(list_name)
            if folder is None:
                folder = root.Folders.Add(list_name, OL_TASKS)
                by_name[list_name] = folder
            key = item.get("key") or ""
            if key:
                dup, its = False, folder.Items  # одна коллекция: новый .Items сбрасывает GetNext
                t = its.GetFirst()
                while t is not None:
                    if (KEY_PREFIX + key) in (t.Body or ""):
                        dup = True
                        break
                    t = its.GetNext()
                if dup:
                    skipped.append(title)
                    continue
            task = folder.Items.Add(3)  # olTaskItem
            task.Subject = title
            notes = item.get("notes") or ""
            if key:
                notes += ("\n" if notes else "") + KEY_PREFIX + key
            task.Body = notes
            if item.get("priority") == "high":
                task.Importance = 2
            if item.get("due"):
                due = datetime.strptime(item["due"], "%Y-%m-%d")
                task.DueDate = due
                hh, mm = (item.get("time") or "10:00").split(":")
                remind = due.replace(hour=int(hh), minute=int(mm))
                soon = datetime.now() + timedelta(minutes=5)
                # без времени в 10:00, но если 10:00 сегодня уже прошло (задача после встречи), через 5 минут
                if not item.get("time") and due.date() == soon.date() and remind < soon:
                    remind = soon.replace(second=0, microsecond=0)
                task.ReminderSet = True
                task.ReminderTime = remind
            task.Save()
            created.append(title)
        except Exception as e:  # noqa: BLE001
            refused.append(f"{title}: {str(e)[:150]}")
    out({"created": created, "skipped_duplicates": skipped, "refused": refused})


def tasks_complete(path):
    """Закрыть открытые задачи по точному заголовку (без учёта регистра); не больше 10 за раз."""
    _, ns = outlook()
    data = json.loads(open(path, encoding="utf-8").read())
    _, folders = task_folders(ns)
    open_tasks = []
    for f in folders:
        items = f.Items
        t = items.GetFirst()
        while t is not None:
            try:
                if t.Class == OL_TASK_CLASS and not t.Complete:
                    open_tasks.append((f.Name, (t.Subject or "").strip(), t))
            except Exception:  # noqa: BLE001
                pass
            t = items.GetNext()
    completed, not_found, ambiguous = [], [], []
    for item in data.get("items", [])[:10]:
        title = (item.get("title") or "").strip()
        lst = item.get("list")
        hits = [x for x in open_tasks if x[1].casefold() == title.casefold() and (not lst or x[0] == lst)]
        if not hits:
            not_found.append(title)
        elif len(hits) > 1:
            ambiguous.append({"title": title, "lists": [h[0] for h in hits]})
        else:
            name, subj, t = hits[0]
            t.MarkComplete()
            t.Save()
            completed.append({"list": name, "title": subj})
            open_tasks.remove(hits[0])
    out({"completed": completed, "not_found": not_found, "ambiguous": ambiguous})


def draft(path):
    app, ns = outlook()
    data = json.loads(open(path, encoding="utf-8").read())
    drafts = ns.GetDefaultFolder(OL_DRAFTS)
    existing = set()
    its = drafts.Items
    d = its.GetFirst()
    while d is not None:
        existing.add((d.Subject or "").strip().lower())
        d = its.GetNext()
    saved, skipped = [], []
    for item in data.get("items", [])[:MAX_DRAFTS]:
        subject = (item.get("subject") or "").strip()
        to = [a for a in item.get("to", []) if a]
        if not to or not subject:
            skipped.append({"subject": subject, "why": "нет адресата или темы"})
            continue
        if subject.lower() in existing:
            skipped.append({"subject": subject, "why": "черновик с такой темой уже есть"})
            continue
        m = app.CreateItem(0)
        m.To = "; ".join(to)
        m.CC = "; ".join(item.get("cc", []))
        m.Subject = subject
        m.Body = item.get("body") or ""
        m.Save()  # только сохранить: Send() в этом скрипте не вызывается нигде
        saved.append(subject)
    out({"drafts_saved": saved, "skipped": skipped})


def doctor():
    _, ns = outlook()
    res = {"stores": [], "tasks_lists": []}
    for s in stores(ns):
        inbox = default_folder(s, OL_INBOX)
        res["stores"].append({"name": s.DisplayName, "inbox_items": inbox.Items.Count if inbox else None})
    root, folders = task_folders(ns)
    res["tasks_lists"] = [f.Name for f in folders]
    res["locale_date_sample"] = (locale.setlocale(locale.LC_TIME, ""), datetime(2026, 11, 3, 14, 5).strftime("%x %H:%M"))
    out(res)


if __name__ == "__main__":
    a = sys.argv[1:]
    try:
        if a[:1] == ["mail"]:
            mail(float(a[1]) if len(a) > 1 else 24, int(a[2]) if len(a) > 2 else 600,
                 a[3] if len(a) > 3 else "inbox", a[4] if len(a) > 4 else None)
        elif a[:1] == ["calendar"]:
            calendar(float(a[1]) if len(a) > 1 else 40, float(a[2]) if len(a) > 2 else 0)
        elif a[:2] == ["tasks", "list"]:
            tasks_list(int(a[2]) if len(a) > 2 else 7)
        elif a[:2] == ["tasks", "done"]:
            tasks_done(int(a[2]) if len(a) > 2 else 7)
        elif a[:2] == ["tasks", "add"] and len(a) > 2:
            tasks_add(a[2])
        elif a[:2] == ["tasks", "complete"] and len(a) > 2:
            tasks_complete(a[2])
        elif a[:1] == ["draft"] and len(a) > 1:
            draft(a[1])
        elif a[:1] == ["doctor"]:
            doctor()
        else:
            print(__doc__)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        out({"error": str(e)[:300]})
        sys.exit(1)
