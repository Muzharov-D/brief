#!/usr/bin/env python3
"""Созвоны из Anarlog (локальная запись и расшифровка на Mac). Только чтение:
используется официальная утилита anarlog, она открывает базу только на чтение.

Использование:
  python3 -I calls.py new [часов=24]   встречи, закончившиеся за N часов, без протокола в Созвоны/
  python3 -I calls.py all [часов=24]   то же, включая уже разобранные
  python3 -I calls.py status           открыта ли программа Anarlog: {running}
Вывод: JSON {since, meetings:[{id, ids, title, started, ended, minutes, event, attendees,
my_notes, transcript, continues?}], pending?}. Перезапуск записи (встреча без названия
сразу после другой) склеивается с ней: ids: все части; continues: протокол, к которому
дописать продолжение; pending: встречи, чья запись ещё идёт, их разбирать позже. В расшифровке «Speaker N»: подписи Anarlog; в протоколе
id встречи отмечается строкой `запись: <id>` (в старых протоколах `anarlog: <id>`), по ней встреча считается разобранной.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ANARLOG = os.path.expanduser("~/.local/bin/anarlog")
PROTOCOLS = (Path(os.environ["BRIEF_WORKSPACE"]) if os.environ.get("BRIEF_WORKSPACE") else Path(__file__).resolve().parent.parent) / "Созвоны"
MIN_WORDS = 40   # короче или меньше MIN_MINUTES: проверка микрофона, а не созвон
MIN_MINUTES = 2


def run(*args):
    if not os.path.exists(ANARLOG):
        sys.exit(json.dumps({"error": "Anarlog не установлен или не включена его команда anarlog (УСТАНОВКА.md, шаг 4)"}, ensure_ascii=False))
    res = subprocess.run([ANARLOG, "--json", *args], capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip()[:500])
    return json.loads(res.stdout)


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def local(dt):
    return dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else ""


def done_ids():
    """{id встречи: файл протокола}."""
    ids = {}
    if PROTOCOLS.exists():
        for f in PROTOCOLS.glob("*.md"):
            for line in re.findall(r"(?:anarlog|запись):\s*([^\n]+)", f.read_text(errors="ignore")):
                for i in re.findall(r"[0-9a-f]{8}-[0-9a-f-]{27}", line):  # все части записи, не только первая
                    ids[i] = str(f)
    return ids


def event_info(raw):
    try:
        ev = json.loads(raw) if raw else {}
    except ValueError:
        return "", []
    title = ev.get("title") or ev.get("name") or ""
    people = []
    for a in ev.get("attendees") or ev.get("participants") or []:
        if isinstance(a, dict):
            name = a.get("name") or a.get("display_name") or ""
            mail = a.get("email") or ""
            people.append(f"{name} <{mail}>".strip() if mail else name)
        elif isinstance(a, str):
            people.append(a)
    return title, [p for p in people if p]


def export(meeting_id, tmp):
    md, js = Path(tmp) / f"{meeting_id}.md", Path(tmp) / f"{meeting_id}.json"
    for fmt, path in (("markdown", md), ("json", js)):
        subprocess.run([ANARLOG, "meetings", "export", meeting_id, "--format", fmt, "-o", str(path), "--force"],
                       capture_output=True, text=True, timeout=120, check=True)
    text = md.read_text()
    transcript = text.split("## Transcript", 1)[1].strip() if "## Transcript" in text else ""
    return transcript, json.loads(js.read_text())


GAP_MINUTES = 15  # запись без названия, начатая так скоро после конца другой, = её продолжение


def groups(listing):
    """Склеивает перезапуски записи: встреча без названия, начатая не позже GAP_MINUTES
    после конца предыдущей, считается её продолжением. Возвращает [[корень, продолжения…]]."""
    items = []
    for m in listing:
        if m.get("status") != "active":
            continue
        start = parse(m.get("started_at")) or parse(m.get("created_at"))
        items.append((start, m))
    items.sort(key=lambda x: x[0] or datetime.min.replace(tzinfo=timezone.utc))
    root_of, out = {}, []
    for start, m in items:
        parent = None
        if not (m.get("title") or "").strip() and start:
            for s2, p in items:
                e2 = parse(p.get("ended_at"))
                if p is m or not e2 or not s2 or s2 >= start:
                    continue
                if timedelta(0) <= start - e2 <= timedelta(minutes=GAP_MINUTES):
                    parent = p
        if parent is not None:
            g = root_of[parent["id"]]
            g.append(m)
        else:
            g = [m]
            out.append(g)
        root_of[m["id"]] = g
    return out


def status():
    """Открыта ли программа Anarlog: запись созвона идёт только в ней, сама она после закрытия не запускается.
    BRIEF_ANARLOG_PROC меняет имя процесса (для тестов)."""
    name = os.environ.get("BRIEF_ANARLOG_PROC") or "anarlog"
    try:
        r = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True, timeout=10)
        running = r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        running = False
    print(json.dumps({"running": running, "app": "Anarlog"}, ensure_ascii=False))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "new"
    if mode == "status":
        return status()
    hours = float(sys.argv[2]) if len(sys.argv) > 2 else 24
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    if not os.path.exists(ANARLOG):
        print(json.dumps({"error": "Нет утилиты anarlog: Anarlog, Settings, Developers, Install"}, ensure_ascii=False))
        return
    done = done_ids() if mode == "new" else {}
    out, pending = [], []
    listing = run("meetings", "list", "--limit", "100")["data"]
    with tempfile.TemporaryDirectory() as tmp:
        for group in groups(listing):
            stale = now - timedelta(hours=3)  # незакрытая запись старше 3 ч: не ждать её
            group = [m for i, m in enumerate(group) if i == 0 or parse(m.get("ended_at"))
                     or (parse(m.get("created_at")) or now) > stale]
            root, last = group[0], group[-1]
            ended = parse(last.get("ended_at"))
            r_start = parse(root.get("started_at")) or parse(root.get("created_at"))
            if not ended and len(group) > 1:
                pending.append({"id": root["id"], "title": root.get("title") or "Без названия",
                                "started": local(r_start), "why": "запись продолжается"})
                continue
            if not ended or ended < since or ended > now:
                continue
            new_parts = [m for m in group if m["id"] not in done]
            if not new_parts:
                continue
            parts, texts, notes, attendees, ev_title, minutes = [], [], [], [], "", 0
            for m in new_parts:
                s = parse(m.get("started_at")) or parse(m.get("created_at"))
                e = parse(m.get("ended_at"))
                if not s or not e or (e - s).total_seconds() < MIN_MINUTES * 60:
                    continue
                transcript, full = export(m["id"], tmp)
                if full.get("capture_pending"):
                    parts = None
                    break
                if len(transcript.split()) < MIN_WORDS:
                    continue
                t, people = event_info(full.get("event_json"))
                ev_title = ev_title or t
                for a in people:
                    if a not in attendees:
                        attendees.append(a)
                for pp in full.get("participants") or []:
                    name = pp.get("display_name") or ""
                    if name and name not in " ".join(attendees):
                        attendees.append(name + (f" ({pp['organization_name']})" if pp.get("organization_name") else ""))
                note = ((full.get("note") or {}).get("markdown") or "").strip()
                if note:
                    notes.append(note)
                if m is not group[0]:
                    transcript = f"--- Продолжение: запись перезапущена в {local(s)[11:]} ---\n{transcript}"
                texts.append(transcript)
                minutes += round((e - s).total_seconds() / 60)
                parts.append({"id": m["id"], "started": local(s), "ended": local(e)})
            if parts is None:
                pending.append({"id": root["id"], "title": root.get("title") or "Без названия",
                                "started": local(r_start), "why": "расшифровка ещё не готова"})
                continue
            if not parts:
                continue
            item = {
                "id": parts[0]["id"],
                "ids": [x["id"] for x in parts],
                "title": root.get("title") or ev_title or "Без названия",
                "started": parts[0]["started"],
                "ended": parts[-1]["ended"],
                "minutes": minutes,
                "event": ev_title,
                "attendees": attendees,
                "my_notes": "\n\n".join(notes),
                "transcript": "\n\n".join(texts),
            }
            if root["id"] in done:
                item["continues"] = done[root["id"]]
            out.append(item)
    out.sort(key=lambda x: x["started"])
    res = {"since": local(since), "meetings": out}
    if pending:
        res["pending"] = pending
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
