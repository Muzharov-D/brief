# -*- coding: utf-8 -*-
"""Инструменты brief для Codex по протоколу MCP (stdio): те же четыре, что в своей обвязке.
Командной строки у Codex нет: всё чтение, запись и вызовы коннекторов идут через этот сервер,
только внутри рабочей папки и с записью в журнал аудита.
Запуск: python -I core/mcp_brief.py <агент> <рабочая_папка>"""
import json
import os
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parent
sys.path.insert(0, str(CORE))
os.environ["BRIEF_AGENT"] = sys.argv[1] if len(sys.argv) > 1 else "codex"
import brief  # noqa: E402
import harness  # noqa: E402

WS = Path(sys.argv[2]) if len(sys.argv) > 2 else brief.workspace(brief.load_profile())
LIMIT = 60000


def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    msg.update({"error": error} if error else {"result": result})
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def call(name, args):
    try:
        out = harness._do(name, args or {}, WS, lambda a: brief.cmd_run(a, capture=True, timeout=600), brief.audit)
    except harness.Refused as e:
        brief.audit("отказ", tool=name, args=[str(args)[:120]], why=str(e))
        out = json.dumps({"error": f"отказано: {e}"}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        out = json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
    out = brief.spill(name, out, LIMIT)  # длинный ответ целиком в файл рабочей папки, не обрезаем
    return {"content": [{"type": "text", "text": out}], "isError": out.startswith('{"error"')}


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        mid, method, params = req.get("id"), req.get("method", ""), req.get("params") or {}
        if mid is None:  # уведомления (initialized и т. п.) ответа не ждут
            continue
        if method == "initialize":
            reply(mid, {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                        "capabilities": {"tools": {}}, "serverInfo": {"name": "brief", "version": "1"}})
        elif method == "tools/list":
            reply(mid, {"tools": [{"name": t["function"]["name"], "description": t["function"]["description"],
                                   "inputSchema": t["function"]["parameters"]} for t in harness.TOOLS]})
        elif method == "tools/call":
            reply(mid, call(params.get("name", ""), params.get("arguments") or {}))
        elif method == "ping":
            reply(mid, {})
        else:
            reply(mid, error={"code": -32601, "message": f"нет метода {method}"})


if __name__ == "__main__":
    main()
