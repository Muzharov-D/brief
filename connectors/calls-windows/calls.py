#!/usr/bin/env python3
"""Созвоны, записанные окном «Запись созвона» (record.pyw) на этом компьютере. Только чтение.

Использование:
  python -I calls.py new [часов=72]   записи, закончившиеся за N часов, без протокола в Созвоны/
  python -I calls.py all [часов=72]   то же, включая уже разобранные
Вывод тот же, что у коннектора Anarlog: JSON {since, meetings:[{id, ids, title, started, ended, minutes,
event, attendees, my_notes, transcript}]}. В расшифровке «Я» это микрофон владельца, «Собеседник» все
остальные участники вместе. Запись считается разобранной, если её id есть в протоколе строкой `запись: <id>`.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

WS = Path(os.environ["BRIEF_WORKSPACE"]) if os.environ.get("BRIEF_WORKSPACE") else Path.home() / "Brief"
RECORDS, PROTOCOLS = WS / "Записи", WS / "Созвоны"
MIN_MINUTES, MIN_WORDS = 2, 40


def read(f):
    """Шапка до пустой строки (Тема, Начало, Конец, Минут, Id) и расшифровка после неё."""
    text = f.read_text(encoding="utf-8", errors="replace")
    head_txt, _, body = text.partition("\n\n")
    head = {k.strip(): v.strip() for k, v in (ln.split(":", 1) for ln in head_txt.splitlines() if ":" in ln)}
    return head, body.strip()


def rec_id(f, head):
    if head.get("Id"):
        return head["Id"]
    digits = re.sub(r"\D", "", f.stem)[:12]  # «2026-11-24 10-00 Тема» из старых записей без Id
    return f"rec-{digits[:8]}-{digits[8:]}" if len(digits) == 12 else "rec-" + re.sub(r"\W+", "-", f.stem).strip("-")


def done_ids():
    ids = set()
    if PROTOCOLS.exists():
        for p in PROTOCOLS.glob("*.md"):
            for line in re.findall(r"(?:запись|anarlog):\s*([^\n]+)", p.read_text(encoding="utf-8", errors="ignore")):
                ids.update(line.split())
    return ids


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "new"
    hours = float(sys.argv[2]) if len(sys.argv) > 2 else 72
    since = datetime.now() - timedelta(hours=hours)
    done = done_ids() if mode == "new" else set()
    out = []
    for f in sorted(RECORDS.glob("*.txt")) if RECORDS.exists() else []:
        if datetime.fromtimestamp(f.stat().st_mtime) < since:
            continue
        head, body = read(f)
        rid = rec_id(f, head)
        try:
            minutes = int(head.get("Минут") or 0)
        except ValueError:
            minutes = 0
        if rid in done or minutes < MIN_MINUTES or len(body.split()) < MIN_WORDS:
            continue
        topic = head.get("Тема", "")
        out.append({"id": rid, "ids": [rid], "title": topic if topic and topic != "Созвон" else "Без названия",
                    "started": head.get("Начало", ""), "ended": head.get("Конец", ""), "minutes": minutes,
                    "event": "", "attendees": [], "my_notes": "", "transcript": body, "file": str(f)})
    out.sort(key=lambda x: x["started"])
    print(json.dumps({"since": since.strftime("%Y-%m-%d %H:%M"), "meetings": out}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    main()
