"""Голосовые и заметки, присланные боту брифингов, в текст (расшифровка на этом компьютере).

Использование: run voice [часов=24]
Вывод: JSON {since, memos:[{source, at, text, key}]}; key стабилен, по нему агент не создаёт дубли задач.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

DATA = Path(os.environ.get("BRIEF_TG_DATA") or (Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local/share")) / "briefing-telegram"))
VOICE_DIR = DATA / "voice"
CACHE = VOICE_DIR / "transcripts.json"
TOOLS = Path(__file__).resolve().parent


def main():
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 24
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    memos, errors = [], []

    r = subprocess.run([sys.executable, "-X", "utf8", str(Path(os.environ["BRIEF_TG_PY"]) if os.environ.get("BRIEF_TG_PY") else TOOLS / "tg.py"), "bot-inbox", str(VOICE_DIR)],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    try:
        data = json.loads(r.stdout)
    except ValueError:
        data = {"error": (r.stderr or r.stdout)[:300]}
    deferred = []
    if "error" in data:
        msg = str(data["error"])
        # краткий обрыв связи не ошибка: сообщения лежат у бота и подтянутся в следующий запуск
        if any(w in msg for w in ("нет связи", "timed out", "timeout", "onnection", "handshake", "urlopen error")):
            deferred.append("бот: нет связи, подтянется в следующий запуск")
        else:
            errors.append("бот: " + msg)
    for it in data.get("items", []):
        if it["at"] < since:
            continue
        who = f", переслано от {it['forwarded_from']}" if it.get("forwarded_from") else ""
        key = f"voice:bot:{it['id']}"
        try:
            if it.get("kind") == "text":
                memos.append({"source": f"бот, текст{who}", "at": it["at"], "text": it["text"], "key": key,
                              **({"reply_to": it["reply_to"]} if it.get("reply_to") else {})})
                continue
            if it.get("error") or not it.get("file"):
                errors.append(f"бот, голосовое {it['id']}: {it.get('error', 'нет файла')}")
                continue
            cid = str(it["id"])
            if cid not in cache:
                import stt
                cache[cid] = stt.text(it["file"])
                CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            memos.append({"source": f"бот, голосовое{who}", "at": it["at"], "text": cache[cid], "key": key, **({"reply_to": it["reply_to"]} if it.get("reply_to") else {})})
        except Exception as e:  # noqa: BLE001: одна плохая запись не должна ронять остальные
            errors.append(f"бот, {it['id']}: {e}")

    memos.sort(key=lambda x: x["at"])
    res = {"since": since, "memos": memos}
    if errors:
        res["errors"] = errors
    if deferred:
        res["deferred"] = deferred
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(TOOLS))
    main()
