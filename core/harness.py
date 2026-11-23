# -*- coding: utf-8 -*-
"""Своя агентная обвязка brief: модель получает задание и узкий набор инструментов, остальное ей недоступно.

Работает с любой моделью, у которой есть OpenAI-совместимый API или API Ollama:
  на этом компьютере: Ollama, LM Studio, MLX (mlx_lm.server), llama.cpp;
  в контуре компании: vLLM, Среда, корпоративный шлюз к GigaChat, YandexGPT и др.
Профиль, поле runtime:
  {"kind": "ollama", "model": "qwen3:8b", "base_url": "http://localhost:11434", "num_ctx": 32768}
  {"kind": "openai", "model": "...", "base_url": "http://localhost:8080/v1", "key_file": "~/.brief/secrets/llm.key"}

Инструменты модели (и только они):
  brief(command)          команды коннекторов из задания, без «run»: "mail 14 600", "tg inbox 16"
  read_file(path)         прочитать файл внутри рабочей папки
  write_file(path, text)  записать файл внутри рабочей папки (удалять нечем)
  list_files(path)        список файлов внутри рабочей папки
Отправить письмо, сообщение другому человеку, удалить, выйти в интернет модель не может: таких инструментов нет.
Каждый вызов пишется в журнал аудита (brief audit).
"""
import json
import os
import re
import shlex
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

TOOLS = [
    {"type": "function", "function": {
        "name": "brief",
        "description": "Запустить команду коннектора из задания. Пиши то, что в задании стоит после `run`, "
                       "например \"mail 14 600\", \"calendar 40\", \"tasks list 7\", \"tg inbox 16\", "
                       "\"tg notify .tmp/msg.html\", \"date\". Ответ обычно JSON.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Прочитать текстовый файл из рабочей папки (путь относительный или полный).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Записать текстовый файл в рабочую папку (создаёт папки). "
                                             "Временные файлы для команд клади в .tmp/",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "list_files", "description": "Список файлов в папке внутри рабочей папки, новые сверху.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
]


class Refused(Exception):
    pass


def _inside(ws, path):
    """Путь внутри рабочей папки или отказ. ~/Brief/…, относительные и полные пути."""
    p = Path(os.path.expanduser(str(path).strip().strip("`\"'")))
    if not p.is_absolute():
        p = ws / p
    p = p.resolve()
    if p != ws.resolve() and ws.resolve() not in p.parents:
        raise Refused(f"путь вне рабочей папки: {path}")
    return p


def _key(rt):
    f = rt.get("key_file")
    if not f:
        return os.environ.get("BRIEF_LLM_KEY", "")
    return Path(os.path.expanduser(f)).read_text(encoding="utf-8").strip()


def _post(url, body, rt, timeout):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    k = _key(rt)
    if k:
        req.add_header("Authorization", f"Bearer {k}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _base(rt):
    default = "http://localhost:11434" if rt.get("kind") == "ollama" else "http://localhost:8080/v1"
    return (rt.get("base_url") or default).rstrip("/")


def ping(rt):
    url = _base(rt) + ("/api/tags" if rt.get("kind") == "ollama" else "/models")
    try:
        req = urllib.request.Request(url)
        k = _key(rt)
        if k:
            req.add_header("Authorization", f"Bearer {k}")
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
        names = [m.get("name") or m.get("id") for m in d.get("models", d.get("data", []))]
        if rt.get("model") and not any(rt["model"] == n or rt["model"] in str(n) for n in names):
            return False, f"модель {rt['model']} не найдена, есть: {', '.join(map(str, names[:5]))}"
        return True, _base(rt)
    except Exception as e:  # noqa: BLE001
        return False, f"{_base(rt)} не отвечает: {e}"


TOOLCALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def _chat(rt, messages):
    """Один шаг модели. Возвращает (текст, [(id, имя, аргументы)], сообщение для истории)."""
    timeout = int(rt.get("timeout", 600))
    if rt.get("kind") == "ollama":
        d = _post(_base(rt) + "/api/chat", {
            "model": rt["model"], "messages": messages, "tools": TOOLS, "stream": False,
            "options": {"num_ctx": int(rt.get("num_ctx", 32768)), "temperature": rt.get("temperature", 0.3)}},
            rt, timeout)
        msg = d.get("message", {})
        calls = [(f"c{i}", c["function"]["name"], c["function"].get("arguments") or {})
                 for i, c in enumerate(msg.get("tool_calls") or [])]
        hist = {"role": "assistant", "content": msg.get("content", ""), **({"tool_calls": msg["tool_calls"]} if calls else {})}
    else:
        body = {"model": rt["model"], "messages": messages, "tools": TOOLS, "tool_choice": "auto",
                "temperature": rt.get("temperature", 0.3), "max_tokens": int(rt.get("max_tokens", 4096))}
        body.update(rt.get("extra_body", {}))
        d = _post(_base(rt) + "/chat/completions", body, rt, timeout)
        msg = d["choices"][0]["message"]
        calls = []
        for c in msg.get("tool_calls") or []:
            args = c["function"].get("arguments") or "{}"
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except ValueError:
                args = {"_raw": args}
            calls.append((c.get("id") or f"c{len(calls)}", c["function"]["name"], args))
        hist = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            hist["tool_calls"] = msg["tool_calls"]
    text = hist.get("content") or ""
    if not calls:  # сервер не разобрал вызовы: модель написала их текстом
        for i, m in enumerate(TOOLCALL_RE.finditer(text)):
            try:
                c = json.loads(m.group(1))
                calls.append((f"t{i}", c.get("name"), c.get("arguments") or {}))
            except ValueError:
                pass
        if calls:
            text = TOOLCALL_RE.sub("", text)
            hist = {"role": "assistant", "content": text}
            hist["_textcalls"] = True
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    return text, calls, hist


def _tool_msg(rt, hist, call_id, name, content):
    if hist.get("_textcalls"):
        return {"role": "user", "content": f"Результат {name}:\n{content}"}
    if rt.get("kind") == "ollama":
        return {"role": "tool", "content": content, "tool_name": name}
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _shrink(messages, budget):
    """Держим историю в пределах контекста модели: старые большие ответы инструментов сокращаем."""
    def size():
        return sum(len(str(m.get("content", ""))) for m in messages)
    for m in messages[2:-4]:
        if size() <= budget:
            break
        c = str(m.get("content", ""))
        if m.get("role") in ("tool", "user") and len(c) > 1200 and m is not messages[1]:
            m["content"] = c[:800] + f"\n…[сокращено ради памяти модели, было {len(c)} символов; при нужде вызови снова]"


def run(prompt, rt, profile, ws, say, brief_run, audit):
    """Выполнить задание. brief_run(args) -> (код, вывод). Возвращает (код, итоговый текст)."""
    ws = Path(ws)
    (ws / ".tmp").mkdir(parents=True, exist_ok=True)
    tool_chars = int(rt.get("tool_chars", 16000))
    budget = int(rt.get("num_ctx", 32768)) * 3  # символов на контекст, грубо 3 символа на токен
    name = profile.get("assistant_name") or "Огонёк"
    pre = (f"Ты {name}, личный ассистент. Сейчас {datetime.now().astimezone():%A %d.%m.%Y %H:%M}. "
           f"Рабочая папка: {ws}. Ты работаешь через инструменты: brief (команды `run …` и `brief run …` из задания, "
           "в command пиши всё после `run`), read_file, write_file, list_files. Других возможностей нет: "
           "ты не отправляешь писем и сообщений другим людям, не удаляешь, не ходишь в интернет. "
           "Где в задании сказано «во временный файл», пиши файл в .tmp/ через write_file и передавай путь в команду. "
           "Вызывай инструменты по одному шагу, смотри результат. Когда всё сделано, ответь итоговым текстом без вызова инструментов. "
           "Пиши по-русски.")
    messages = [{"role": "system", "content": pre}, {"role": "user", "content": prompt}]
    t_end = time.time() + int(rt.get("time_limit", 2400))
    nudges, checked, final = 0, False, ""
    for step in range(int(rt.get("max_steps", 40))):
        if time.time() > t_end:
            return 1, "время на задание вышло"
        _shrink(messages, budget)
        sent = sum(len(str(m.get("content", ""))) for m in messages)
        try:
            text, calls, hist = _chat(rt, messages)
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            say(f"модель не ответила: {e}")
            return 1, f"модель не ответила: {e}"
        audit("model", model=rt.get("model"), endpoint=_base(rt), sent=sent, got=len(text))
        messages.append(hist)
        if not calls:
            if text and rt.get("self_check", True) and not checked:
                checked = True  # одна сверка с заданием: маленькие модели пропускают пункты
                messages.append({"role": "user", "content": "Сверь сделанное с заданием по пунктам. Если какой-то пункт "
                                 "пропущен (файл не записан, команда не вызвана), выполни его сейчас инструментами. "
                                 "Если всё сделано, ответь итоговым текстом ещё раз, без изменений."})
                final = text
                continue
            if text or nudges >= 2:
                return (0, text or final) if (text or final) else (1, "модель закончила без итога")
            nudges += 1  # маленькие модели иногда замолкают после ошибки инструмента
            messages.append({"role": "user", "content": "Задание не закончено. Продолжай по пунктам задания: "
                             "если команда вернула error, отметь это одной строкой и переходи к следующему пункту. "
                             "В конце дай итоговый текст."})
            continue
        for cid, tname, args in calls:
            try:
                out = _do(tname, args or {}, ws, brief_run, audit)
            except Refused as e:
                out = json.dumps({"error": f"отказано: {e}"}, ensure_ascii=False)
                audit("отказ", tool=tname, args=[str(args)[:120]], why=str(e))
            except Exception as e:  # noqa: BLE001
                out = json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
            if len(out) > tool_chars:
                out = out[:tool_chars] + f"\n…[обрезано: всего {len(out)} символов; сузь запрос: меньше часов, фильтр]"
            shown = {k: v for k, v in (args or {}).items() if k != "content"}  # текст файла в журнал не пишем
            say(f"шаг {step + 1}: {tname} {json.dumps(shown, ensure_ascii=False)[:160]} -> {len(out)} симв.")
            messages.append(_tool_msg(rt, hist, cid, tname, out))
    return 1, "превышено число шагов"


def _do(tname, args, ws, brief_run, audit):
    if tname == "brief":
        cmd = str(args.get("command") or args.get("_raw") or "").strip()
        cmd = re.sub(r"^(~/\S*/)?(brief\s+)?run\s+", "", cmd)
        parts = shlex.split(cmd)
        if not parts:
            raise Refused("пустая команда")
        for a in parts[1:]:  # аргументы-пути только внутри рабочей папки
            if "/" in a or re.search(r"\.(json|txt|md|html)$", a):
                p = _inside(ws, a)
                parts[parts.index(a)] = str(p)
        code, out = brief_run(parts)
        return out if out.strip() else f"(код {code}, пустой ответ)"
    if tname == "read_file":
        p = _inside(ws, args.get("path", ""))
        audit("read", path=str(p.relative_to(ws.resolve())))
        if not p.exists():
            return json.dumps({"error": "файла нет"}, ensure_ascii=False)
        return p.read_text(encoding="utf-8", errors="replace")
    if tname == "write_file":
        p = _inside(ws, args.get("path", ""))
        text = str(args.get("content", ""))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        audit("write", path=str(p.relative_to(ws.resolve())), bytes=len(text.encode("utf-8")))
        return json.dumps({"ok": True, "path": str(p)}, ensure_ascii=False)
    if tname == "list_files":
        p = _inside(ws, args.get("path") or ".")
        if not p.is_dir():
            return json.dumps({"error": "папки нет"}, ensure_ascii=False)
        items = sorted(p.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)[:60]
        return "\n".join(x.name + ("/" if x.is_dir() else "") for x in items) or "(пусто)"
    raise Refused(f"нет такого инструмента: {tname}")
