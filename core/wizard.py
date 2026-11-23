# -*- coding: utf-8 -*-
"""Мастер настройки brief: страница в браузере на этом компьютере.

Запуск: brief setup            откроет браузер
        brief setup --port 8790 --no-open
Сервер слушает только 127.0.0.1 и отвечает только на запросы с ключом из адреса страницы:
другие сайты в браузере не могут ничего прочитать или поменять.
Всё, что делает мастер, делается командами brief, поэтому попадает в журнал аудита.
"""
import json
import os
import secrets
import shlex
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CORE = Path(__file__).resolve().parent
sys.path.insert(0, str(CORE))
import brief  # noqa: E402

PAGE = CORE / "wizard"
BRIEF = [sys.executable, "-I", str(CORE / "brief.py")]  # без bash: так же работает и на Windows
TOKEN = os.environ.get("BRIEF_WIZARD_TOKEN") or secrets.token_urlsafe(18)
JOBS = {}


def sh(args, timeout=300):
    """Команда brief; ответ JSON, если он JSON, иначе текст."""
    try:
        r = subprocess.run([*BRIEF, *args], capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                           env=dict({k: v for k, v in os.environ.items() if k != "BRIEF_AGENT"}, BRIEF_ACTOR="мастер настройки"))
        out = r.stdout.strip()
        try:
            return json.loads(out)
        except ValueError:
            return {"text": out or r.stderr.strip()[-1500:], "code": r.returncode}
    except subprocess.TimeoutExpired:
        return {"error": "не дождались ответа"}


def local_models():
    """Какие программы с моделями уже запущены на этом компьютере."""
    found = []
    for kind, name, url, base in (("ollama", "Ollama", "http://127.0.0.1:11434/api/tags", "http://127.0.0.1:11434"),
                                  ("openai", "LM Studio", "http://127.0.0.1:1234/v1/models", "http://127.0.0.1:1234/v1"),
                                  ("openai", "MLX", "http://127.0.0.1:8080/v1/models", "http://127.0.0.1:8080/v1")):
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                d = json.loads(r.read().decode("utf-8"))
            for m in d.get("models", d.get("data", [])):
                found.append({"app": name, "kind": kind, "base_url": base, "model": m.get("name") or m.get("id")})
        except Exception:  # noqa: BLE001
            pass
    return found


def codex_status():
    r = subprocess.run([str(CORE / "codex-path")], capture_output=True, text=True)
    if r.returncode:
        return {"found": False}
    ok = subprocess.run([r.stdout.strip(), "login", "status"], capture_output=True).returncode == 0
    return {"found": True, "logged_in": ok}


def state():
    p = brief.load_profile()
    cons = {cid: c for cid, c in brief.connectors().items() if brief.PLATFORM in c.get("platforms", [brief.PLATFORM])}
    agents = []
    for name, md in brief.agent_files(p).items():
        s = brief.agent_settings(name, md, p)
        tried = brief.dry_last(name)
        agents.append({"id": name, "title": s["title"], "schedule": s["schedule"], "on": s["on"],
                       "description": s["description"], "source": s["source"],
                       "tried": tried and {"at": tried.get("at"), "ok": tried.get("ok")}})
    tg = sh(["run", "tg", "status"], 30) if "telegram" in {c for _, c in brief.bound_pairs(p)} else {}
    return {"profile": p, "platform": brief.PLATFORM, "connectors": cons, "agents": agents, "telegram": tg,
            "workspace": str(brief.workspace(p))}


def merge(patch):
    p = brief.load_profile()
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(p.get(k), dict) and k not in ("runtime", "fallback_runtime"):
            p[k].update(v)
            p[k] = {a: b for a, b in p[k].items() if b is not None}
        elif v is None:
            p.pop(k, None)
        else:
            p[k] = v
    p.setdefault("workspace", "~/Brief")
    brief.save_profile(p)
    ws = brief.workspace(p)
    for d in ("Брифинги", "Созвоны", "Люди", "Агенты", "Журнал", ".tmp"):
        (ws / d).mkdir(parents=True, exist_ok=True)
    brief.private(ws)
    for f in (brief.ROOT / "templates").rglob("*.md"):  # заготовки своих файлов, существующие не трогаем
        dst = ws / f.relative_to(brief.ROOT / "templates")
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    return p


def save_secret(name, value):
    d = brief.HOME / "secrets"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    f = d / name
    f.write_text(value.strip() + "\n", encoding="utf-8")
    f.chmod(0o600)
    return str(f)


def start_job(key, args, timeout=900):
    def work():
        JOBS[key] = {"running": True}
        JOBS[key] = {"running": False, "result": sh(args, timeout)}
    threading.Thread(target=work, daemon=True).start()
    return {"started": key}


def api(name, body):
    if name == "state":
        return state()
    if name == "save":
        return {"profile": merge(body)}
    if name == "models":
        return {"local": local_models(), "codex": codex_status(), "claude": brief.claude_status()}
    if name == "model_key":  # ключ к серверу модели компании: в файл с правами 600, в профиль только путь
        return {"key_file": save_secret("llm.key", body.get("key", ""))}
    if name == "model_check":
        rt = body.get("runtime") or brief.load_profile().get("runtime") or {}
        if rt.get("kind") == "claude":
            st = brief.claude_status()
            if not st.get("found"):
                return {"ok": False, "detail": "Claude Code ещё не установлен: нажмите «Войти в Claude»"}
            if st.get("logged_in") and "api" in str(st.get("method", "")).lower():
                return {"ok": False, "detail": "Claude Code вошёл не по подписке, а с ключом API: войдите заново аккаунтом Claude"}
            return {"ok": st.get("logged_in", False), "detail": "вход в Claude выполнен" if st.get("logged_in")
                    else "нужно войти в Claude: нажмите «Войти в Claude»"}
        if rt.get("kind") == "codex":
            st = codex_status()
            return {"ok": st.get("logged_in", False), "detail": "вход в ChatGPT выполнен" if st.get("logged_in")
                    else "нужно войти в ChatGPT"}
        import harness
        ok, detail = harness.ping(rt)
        return {"ok": ok, "detail": detail}
    if name == "claude_login":  # окно Терминала: при нужде ставит Claude Code с claude.ai, потом вход в браузере
        st = brief.claude_status()
        f = brief.HOME / "state" / "Войти в Claude.command"
        f.parent.mkdir(parents=True, exist_ok=True)
        exe = shlex.quote(st["path"]) if st.get("found") else '"$HOME/.local/bin/claude"'
        install = "" if st.get("found") else (
            'echo "Ставлю Claude Code с сайта claude.ai (около минуты)…"\n'
            'curl -fsSL https://claude.ai/install.sh | bash || { echo "Не получилось поставить. Проверьте интернет."; exit 1; }\n')
        f.write_text("#!/bin/bash\nclear\n"
                     'echo "Вход в Claude для помощника. Откроется браузер: войдите тем же аккаунтом, что и в чате Claude."\n'
                     'echo "Нужна подписка Pro или Max: на бесплатном тарифе помощник работать не сможет."; echo\n'
                     + install + f"{exe} auth login --claudeai\n"
                     'echo; echo "Готово. Вернитесь в окно настройки и нажмите «Проверить и дальше». Это окно можно закрыть."\n',
                     encoding="utf-8")
        f.chmod(0o700)
        subprocess.Popen(["open", "-a", "Terminal", str(f)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"started": True}
    if name == "codex_login":
        st = codex_status()
        if not st.get("found"):
            return {"error": "Codex не найден: обновите приложение ChatGPT"}
        path = subprocess.run([str(CORE / "codex-path")], capture_output=True, text=True).stdout.strip()
        subprocess.Popen([path, "login"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"started": True}
    if name == "tg":  # действия Telegram при настройке: вход, папки, бот
        action = body.get("action")
        if action == "status":
            return sh(["run", "tg", "status"], 30)
        if action == "folders":
            return sh(["run", "tg", "folders"], 120)
        if action == "save_folders":
            return sh(["run", "tg", "access-save", *body.get("folders", [])], 30)
        if action in ("gui-login", "bot-setup"):
            return start_job("tg-" + action, ["run", "tg", action], 900)
    if name == "bitrix_setup":
        return start_job("bitrix", ["run", "tasks", "setup"], 600)
    if name == "job":
        return JOBS.get(body.get("key"), {"running": False, "result": None})
    if name == "doctor":
        return sh(["doctor"], 600)
    if name == "schedule":
        return sh(["schedule", "install"], 120) if brief.PLATFORM == "mac" else {"text": "расписание Windows ставится установщиком"}
    if name == "trial":  # пробный прогон: ничего не отправляет и не меняет, результат в отчёте
        agent = body.get("agent", "morning-briefing")
        return start_job("trial-" + agent, ["agent", agent, "dry"], 2400)
    if name == "trial_report":
        return {"report": brief.dry_last(body.get("agent", ""))}
    if name == "enable":  # агенты включаются по одному: свой переключатель и время, расписание пересобирается сразу
        agent = body.get("agent", "")
        p = brief.load_profile()
        if agent not in brief.agent_files(p):
            return {"error": f"нет агента {agent}"}
        conf = dict((p.get("agents") or {}).get(agent, {}))
        conf.update({k: body[k] for k in ("on", "schedule") if k in body})
        merge({"agents": {agent: conf}})
        if brief.PLATFORM != "mac":
            return {"text": "расписание Windows ставится установщиком"}
        return sh(["schedule", "install"], 120)
    if name == "audit":
        return sh(["audit", str(int(body.get("days", 1)))], 30)
    if name == "feed":  # главная после настройки: то, что ушло в Telegram, и состояние агентов
        return {"items": brief.feed(int(body.get("days", 7))), "agents": brief.agent_status(),
                "schedule": brief.PLATFORM == "mac" and brief.schedule_owner() is not None and brief.schedule_mine()}
    return {"error": f"нет такого действия: {name}"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, data, ctype="application/json; charset=utf-8"):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        files = {"/": ("index.html", "text/html; charset=utf-8"), "/ogonyok.svg": ("ogonyok.svg", "image/svg+xml")}
        if path not in files:
            return self._send(404, {"error": "нет"})
        name, ctype = files[path]
        self._send(200, (PAGE / name).read_bytes(), ctype)

    def do_POST(self):
        if self.headers.get("X-Brief-Token") != TOKEN or not self.path.startswith("/api/"):
            return self._send(403, {"error": "нет доступа"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            self._send(200, api(self.path[5:], body))
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main(argv):
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 0
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{srv.server_address[1]}/?t={TOKEN}"
    print(f"Мастер настройки открыт: {url}\nЗакройте это окно, когда закончите.", flush=True)
    if "--no-open" not in argv:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main(sys.argv[1:])
