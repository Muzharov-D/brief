# -*- coding: utf-8 -*-
"""Нужно ли сейчас запускать частого агента. Модель стартует, только когда есть дело: это экономит лимит
подписки (Codex, Claude) и не гоняет локальную модель впустую. Агенты без проверки запускаются всегда."""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

HOME = Path(os.environ.get("BRIEF_HOME") or (Path.home() / ".brief"))
STATE = HOME / "state" / "gates.json"


def _load():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _json(res):
    code, out = res
    try:
        return json.loads(out)
    except ValueError:
        return {"error": out.strip()[:200] or f"код {code}"}


def bot_inbox(state, tool):
    voice = Path(os.environ.get("BRIEF_TG_DATA") or (HOME / "data/telegram")) / "voice"
    data = _json(tool("tg", "bot-inbox", str(voice)))
    if "error" in data:
        return False, f"бот: {data['error']}"
    top = max([it["id"] for it in data.get("items", [])] + [0])
    if top <= state.get("bot-inbox", 0):
        return False, "новых сообщений боту нет"
    state["bot-inbox:pending"] = top
    return True, f"новые сообщения боту до id {top}"


def meeting_prep(state, tool):
    data = _json(tool("calendar", "2"))
    if "error" in data:
        return False, f"календарь: {data['error']}"
    now = datetime.now().astimezone()
    done, soon = set(state.get("meeting-prep", [])), []
    for e in data.get("events", []):
        try:
            start = datetime.fromisoformat(e["start"])
        except (KeyError, ValueError):
            continue
        key = f"{e.get('title', '')}|{e['start']}"
        if e.get("canceled") or e.get("all_day") or key in done:
            continue
        if timedelta(minutes=15) <= start - now <= timedelta(minutes=75):
            soon.append(key)
    if not soon:
        return False, "встреч через 15-75 минут нет"
    state["meeting-prep:pending"] = soon
    return True, f"скоро встречи: {len(soon)}"


def after_call(state, tool):
    data = _json(tool("calls", "new", "6"))
    if "error" in data:
        return False, f"созвоны: {data['error']}"
    if not data.get("meetings"):
        return False, "новых записей созвонов нет"
    return True, f"записей для протокола: {len(data['meetings'])}"


CHECKS = {"bot-inbox": bot_inbox, "meeting-prep": meeting_prep, "after-call": after_call}


def check(agent, tool):
    if agent not in CHECKS:
        return True, ""
    state = _load()
    go, why = CHECKS[agent](state, tool)
    _save(state)
    return go, why


def commit(agent):
    state = _load()
    if agent == "bot-inbox" and "bot-inbox:pending" in state:
        state["bot-inbox"] = state.pop("bot-inbox:pending")
    if agent == "meeting-prep" and "meeting-prep:pending" in state:
        state["meeting-prep"] = (state.get("meeting-prep", []) + state.pop("meeting-prep:pending"))[-200:]
    _save(state)
