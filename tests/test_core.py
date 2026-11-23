# -*- coding: utf-8 -*-
"""Проверка ядра и обвязки без настоящих сервисов: копия ядра, поддельный коннектор, поддельная модель.
Запуск: python3 -m unittest discover -s tests"""
import json
from datetime import datetime
import re
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def call(name, args, i):
    return {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


SCRIPT = [
    [call("brief", {"command": "date"}, 1)],
    [call("write_file", {"path": ".tmp/msg.txt", "content": "привет"}, 2)],
    [call("brief", {"command": "run fake echo .tmp/msg.txt"}, 3)],
    [call("read_file", {"path": "/etc/hosts"}, 4)],
    [call("brief", {"command": "fake echo ../../../etc/hosts"}, 5)],
    [call("brief", {"command": "fake login"}, 6)],
    [call("delete_file", {"path": "Контекст.md"}, 7)],
    "Итог: всё сделано.",
]


class Model(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        self._send({"data": [{"id": "fake-model"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Model.seen.append(body)
        step = SCRIPT[min(len(Model.seen) - 1, len(SCRIPT) - 1)]
        if isinstance(step, str):
            msg = {"role": "assistant", "content": "<think>служебное</think>" + step}
        else:
            msg = {"role": "assistant", "content": "", "tool_calls": step}
        self._send({"choices": [{"message": msg}]})

    def _send(self, d):
        data = json.dumps(d, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        shutil.copytree(REPO, self.repo, ignore=shutil.ignore_patterns(".git", "design", "tests"))
        fake = self.repo / "connectors/fake"
        fake.mkdir()
        (fake / "connector.json").write_text(json.dumps({
            "id": "fake", "title": "Тест", "provides": {"fake": "fake.py"}, "setup_only": ["login"],
            "check": ["fake", "echo"], "purpose": "", "can": [], "cannot": [], "data": ""}), encoding="utf-8")
        (fake / "fake.py").write_text(
            "import json,sys\nprint(json.dumps({'args': sys.argv[1:], 'text': open(sys.argv[2]).read() if len(sys.argv)>2 else ''}, ensure_ascii=False))\n",
            encoding="utf-8")
        self.home = self.tmp / "home"
        self.ws = self.tmp / "ws"
        (self.ws / "Агенты").mkdir(parents=True)
        (self.ws / "Агенты/тест.md").write_text("---\ntitle: Тест\n---\nСделай тест для {ИМЯ}.\n", encoding="utf-8")
        self.srv = HTTPServer(("127.0.0.1", 0), Model)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        Model.seen = []
        self.env = dict(os.environ, BRIEF_HOME=str(self.home))
        self.env.pop("BRIEF_AGENT", None)
        self.brief("profile", "name", "Сергей")
        self.brief("profile", "workspace", str(self.ws))
        self.brief("profile", "connectors", json.dumps({"fake": "fake"}))
        self.brief("profile", "runtime", json.dumps({"kind": "openai", "model": "fake-model",
                                                     "base_url": f"http://127.0.0.1:{self.srv.server_port}/v1"}))

    def tearDown(self):
        self.srv.shutdown()
        shutil.rmtree(self.tmp)

    def brief(self, *args, env=None):
        return subprocess.run([sys.executable, "-I", str(self.repo / "core/brief.py"), *args], capture_output=True,
                              text=True, env=env or self.env, timeout=120)

    def audit(self):
        rows = []
        for f in (self.home / "audit").glob("*.jsonl"):
            rows += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()]
        return rows

    def test_profile_private(self):
        self.assertEqual(oct((self.home / "profile.json").stat().st_mode & 0o777), "0o600")

    def test_run_and_audit(self):
        r = self.brief("run", "fake", "echo")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["args"], ["echo"])
        self.assertEqual(self.audit()[-1]["agent"], "человек")

    def test_not_connected(self):
        r = self.brief("run", "mail", "1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("не подключено", r.stdout)

    def test_setup_only_blocked_for_agents(self):
        r = self.brief("run", "fake", "login", env=dict(self.env, BRIEF_AGENT="x"))
        self.assertIn("только человек", r.stdout)
        r = self.brief("run", "fake", "login")
        self.assertEqual(r.returncode, 0)

    def test_agent_harness(self):
        r = self.brief("agent", "тест", "force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        last = (self.ws / "Журнал/тест/последний.md").read_text(encoding="utf-8")
        self.assertEqual(last, "Итог: всё сделано.")
        self.assertIn("Сделай тест для Сергей.", Model.seen[0]["messages"][1]["content"])
        tool_results = [m["content"] for m in Model.seen[-1]["messages"] if m["role"] == "tool"]
        self.assertIn("привет", tool_results[2])                 # файл из .tmp дошёл до коннектора
        self.assertIn("вне рабочей папки", tool_results[3])      # /etc/hosts не читается
        self.assertIn("вне рабочей папки", tool_results[4])      # и через аргумент коннектора
        self.assertIn("только человек", tool_results[5])         # команды настройки агенту закрыты
        self.assertIn("нет такого инструмента", tool_results[6])  # удалять нечем
        a = self.audit()
        self.assertTrue(any(x["event"] == "отказ" for x in a))
        self.assertTrue(any(x["event"] == "write" and x["path"] == ".tmp/msg.txt" for x in a))
        self.assertTrue(all(x["agent"] == "тест" for x in a if x["event"] in ("run", "model", "write")))
        out = self.brief("audit").stdout
        self.assertIn("ОТКАЗАНО", out)

    def test_doctor_and_lists(self):
        out = self.brief("doctor").stdout
        self.assertIn("ок   Модель: fake-model", out)
        self.assertIn("ок   Тест", out)
        self.assertIn("тест", self.brief("agents").stdout)
        self.assertIn("Тест", self.brief("connectors").stdout)


    def test_tasks_in_two_systems(self):
        for cid in ("taskA", "taskB"):
            d = self.repo / "connectors" / cid
            d.mkdir()
            (d / "connector.json").write_text(json.dumps({
                "id": cid, "title": cid, "provides": {"tasks": "t.py"}, "check": ["tasks", "list", "1"],
                "usage": {"tasks": "tasks list | add <файл>" + (" | proposals" if cid == "taskB" else "")},
                "purpose": "", "can": [], "cannot": [], "data": ""}), encoding="utf-8")
            (d / "t.py").write_text(
                "import json,sys\nprint(json.dumps({'who': '" + cid + "', 'args': sys.argv[1:2], "
                "'items': json.load(open(sys.argv[2]))['items'] if sys.argv[1] == 'add' else []}, ensure_ascii=False))\n",
                encoding="utf-8")
        self.brief("profile", "connectors", json.dumps({"tasks": ["taskA", "taskB"]}))
        out = json.loads(self.brief("run", "tasks", "list", "7").stdout)
        self.assertEqual((out["taskA"]["who"], out["taskB"]["who"]), ("taskA", "taskB"))
        self.assertIn("system", out["как_устроено"])
        (self.ws / ".tmp").mkdir(exist_ok=True)
        f = self.ws / ".tmp/new.json"
        f.write_text(json.dumps({"items": [{"title": "себе"}, {"title": "в Б", "system": "taskB"}]}, ensure_ascii=False),
                     encoding="utf-8")
        out = json.loads(self.brief("run", "tasks", "add", str(f)).stdout)
        self.assertEqual(out["taskA"]["items"], [{"title": "себе"}])
        self.assertEqual(out["taskB"]["items"], [{"title": "в Б"}])
        self.assertEqual(json.loads(self.brief("run", "tasks", "proposals").stdout)["who"], "taskB")
        doc = self.brief("doctor").stdout
        self.assertIn("ок   taskA", doc)
        self.assertIn("ок   taskB", doc)
        self.assertIn("[подключено] taskB", self.brief("connectors").stdout)

    def test_claude_subscription(self):
        fake = self.tmp / "claude"
        rec = self.tmp / "claude-call.json"
        fake.write_text("#!" + sys.executable + "\nimport json,os,sys\n"
                        "if sys.argv[1:3] == ['auth', 'status']:\n"
                        "    print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'subscriptionType': 'pro'})); sys.exit()\n"
                        f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'stdin': sys.stdin.read(), "
                        f"'key': os.environ.get('ANTHROPIC_API_KEY')}}, open({str(rec)!r}, 'w'), ensure_ascii=False)\n"
                        "print('Итог от Claude')\n", encoding="utf-8")
        fake.chmod(0o755)
        env = dict(self.env, CLAUDE_BIN=str(fake), ANTHROPIC_API_KEY="чужой-ключ")
        self.brief("profile", "runtime", json.dumps({"kind": "claude"}))
        r = self.brief("agent", "тест", "force", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.ws / "Журнал/тест/последний.md").read_text(encoding="utf-8"), "Итог от Claude")
        c = json.loads(rec.read_text(encoding="utf-8"))
        a = c["argv"]
        for x in ("-p", "--safe-mode", "dontAsk", "Bash(brief run *)", f"Read(/{self.ws.resolve()}/**)"):
            self.assertIn(x, a)
        self.assertNotIn("WebFetch", a[a.index("--tools") + 1])
        self.assertEqual(Path(c["cwd"]).resolve(), self.ws.resolve())
        self.assertIn("Сделай тест для Сергей.", c["stdin"])
        self.assertIsNone(c["key"])  # ключ API из окружения не уходит: только подписка
        self.assertIn("ок   Модель: подписка Claude  pro", self.brief("doctor", env=env).stdout)

    def test_dry_run_changes_nothing(self):
        spec = json.loads((self.repo / "connectors/fake/connector.json").read_text(encoding="utf-8"))
        spec["writes"] = {"fake": ["send"]}
        (self.repo / "connectors/fake/connector.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        bindir = self.home / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        (bindir / "brief").write_text(f"#!/bin/bash\nexec {sys.executable} -I {self.repo / 'core/brief.py'} \"$@\"\n")
        (bindir / "brief").chmod(0o755)
        (self.ws / "заметки.md").write_text("старое", encoding="utf-8")
        fake = self.tmp / "claude"
        fake.write_text("#!" + sys.executable + r"""
import json, os, subprocess, sys
if sys.argv[1:3] == ['auth', 'status']:
    print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai'})); sys.exit()
os.makedirs('.tmp', exist_ok=True)
open('.tmp/msg.txt', 'w').write('текст брифинга')
r1 = subprocess.run(['brief', 'run', 'fake', 'send', '.tmp/msg.txt'], capture_output=True, text=True)
r2 = subprocess.run(['brief', 'run', 'fake', 'echo'], capture_output=True, text=True)
os.makedirs('Брифинги', exist_ok=True)
open('Брифинги/пробный.md', 'w').write('# Брифинг')
open('заметки.md', 'a').write('\nдописал')
print(json.dumps([json.loads(r1.stdout), json.loads(r2.stdout)], ensure_ascii=False))
""", encoding="utf-8")
        fake.chmod(0o755)
        self.brief("profile", "runtime", json.dumps({"kind": "claude"}))
        r = self.brief("agent", "тест", "dry", env=dict(self.env, CLAUDE_BIN=str(fake)))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # настоящая папка не тронута
        self.assertEqual((self.ws / "заметки.md").read_text(encoding="utf-8"), "старое")
        self.assertFalse((self.ws / "Брифинги/пробный.md").exists())
        self.assertFalse((self.ws / "Журнал/тест").exists())
        d = self.home / "state/dry/тест"
        self.assertFalse((d / "Brief").exists())  # копию личных файлов не держим
        rep = json.loads((d / "отчёт.json").read_text(encoding="utf-8"))
        self.assertTrue(rep["ok"])
        self.assertEqual([(a["cap"], a["args"][0], a["content"]) for a in rep["actions"]],
                         [("fake", "send", "текст брифинга")])  # отправка перехвачена вместе с текстом
        answer = json.loads(rep["answer"])
        self.assertTrue(answer[0]["dry_run"])
        self.assertEqual(answer[1]["args"], ["echo"])  # чтение выполняется по-настоящему
        files = {f["path"]: f for f in rep["files"]}
        self.assertTrue(files["Брифинги/пробный.md"]["new"])
        self.assertIn("дописал", files["заметки.md"]["text"])
        ev = [(x["event"], x.get("cap")) for x in self.audit()]
        self.assertIn(("пробно", "fake"), ev)
        self.assertFalse([x for x in self.audit() if x["event"] == "run" and x.get("args", [""])[0] == "send"])

    def test_long_output_goes_to_file(self):
        """Длинный ответ коннектора агенту целиком в файле рабочей папки: Claude Code прячет хвост вне её."""
        spec = json.loads((self.repo / "connectors/fake/connector.json").read_text(encoding="utf-8"))
        (self.repo / "connectors/fake/fake.py").write_text(
            "import json\nprint(json.dumps({'items': [{'n': i, 'text': 'запись ' * 20} for i in range(800)]}, ensure_ascii=False))\n",
            encoding="utf-8")
        r = self.brief("run", "fake", "x", env=dict(self.env, BRIEF_AGENT="тест"))
        ptr = json.loads(r.stdout)
        f = Path(ptr["ответ_в_файле"])
        self.assertEqual(f.parent.resolve(), (self.ws / ".tmp").resolve())
        self.assertEqual(len(json.loads(f.read_text(encoding="utf-8"))["items"]), 800)
        self.assertGreater(ptr["строк"], 800)  # по строкам, чтобы читать частями
        self.assertEqual(len(json.loads(self.brief("run", "fake", "x").stdout)["items"]), 800)  # человеку как есть

    def test_schedule_of_other_install_untouched(self):
        """Тест или вторая установка с другим BRIEF_HOME не снимают и не переставляют чужое расписание."""
        sys.path.insert(0, str(self.repo / "core"))
        os.environ["BRIEF_HOME"] = str(self.home)
        import importlib
        import brief
        importlib.reload(brief)
        brief.schedule_owner = lambda: "/Users/кто-то/.brief/helpers/Огонёк.app/Contents/MacOS/ogonyok"
        self.assertFalse(brief.schedule_mine())
        with self.assertRaises(SystemExit) as e:
            brief.cmd_schedule(["remove"])
        self.assertIn("другая установка", str(e.exception))
        brief.schedule_owner = lambda: str(self.home / "helpers/Огонёк.app/Contents/MacOS/ogonyok")
        self.assertTrue(brief.schedule_mine())

    def test_every_write_command_is_listed(self):
        """Всё, что отправляет или меняет, отмечено в writes: иначе пробный прогон выполнил бы это по-настоящему."""
        risky = {"add", "complete", "retime", "confirm", "reject", "notify", "draft"}
        for f in (REPO / "connectors").glob("*/connector.json"):
            spec = json.loads(f.read_text(encoding="utf-8"))
            for cap, usage in spec.get("usage", {}).items():
                w = spec.get("writes", {}).get(cap, [])
                words = set(re.findall(r"[a-z]+", usage.split("  ")[0]))
                missing = {x for x in risky & (words | {cap}) if "*" not in w and x not in w}
                self.assertFalse(missing, f"{f.parent.name}: {cap} {missing} не в writes")

    def test_why_failed(self):
        sys.path.insert(0, str(self.repo / "core"))
        os.environ["BRIEF_HOME"] = str(self.home)
        import importlib
        import brief
        importlib.reload(brief)
        w = brief.why_failed
        self.assertEqual(w("Claude AI usage limit reached|1795430000")[0], "limit")
        self.assertIn("снова можно в", w("Claude AI usage limit reached|1795430000")[1])
        self.assertEqual(w("ERROR: You've hit your usage limit. Try again in 2 hours 13 minutes.")[1],
                         "снова можно через 2 ч 13 мин")
        self.assertEqual(w("5-hour limit reached ∙ resets 3pm")[1], "сброс в 3pm")
        self.assertEqual(w("Invalid API key · Please run /login")[0], "auth")
        self.assertEqual(w("ERROR: unexpected status 401 Unauthorized")[0], "auth")
        self.assertEqual(w("API Error: 529 Overloaded")[0], "down")
        self.assertEqual(w("модель не ответила: <urlopen error [Errno 61] Connection refused>")[0], "down")
        self.assertEqual(w("не найден Claude Code: откройте настройку")[0], "setup")
        self.assertEqual(w("что-то странное\nвторая строка"), ("other", "что-то странное"))
        self.assertIn("кончился лимит подписки Claude", brief.reasons([({"kind": "claude"}, ("limit", ""))]))

    def _fallback_env(self, mode):
        """Поддельный Claude с заданным поведением, поддельные календарь, задачи и Telegram (notify пишет в файл)."""
        today = datetime.now().strftime("%Y-%m-%d")
        for cid, cap, code in (
                ("fcal", "calendar", "print(json.dumps({'events': [{'start': '" + today + "T10:00:00+03:00', "
                                     "'title': 'Созвон с клубом'}, {'start': '" + today + "T12:00:00+03:00', "
                                     "'title': 'Отменили', 'canceled': True}]}, ensure_ascii=False))"),
                ("ftask", "tasks", "print(json.dumps({'due': [{'title': 'Смета Петрову', 'due': '" + today + "', "
                                   "'overdue_days': 2}, {'title': 'Потом', 'due': '2099-01-01'}]}, ensure_ascii=False))"),
                ("ftg", "tg", "c = sys.argv[1]\n"
                              "if c == 'notify': open(os.environ['REC'], 'a').write(open(sys.argv[2]).read() + '\\n=====\\n'); "
                              "print(json.dumps({'sent': 1}))\n"
                              "else: print(json.dumps({'awaiting_reply': [{'chat': 'Петров'}], 'unread_not_opened': 2}, "
                              "ensure_ascii=False))")):
            d = self.repo / "connectors" / cid
            d.mkdir(exist_ok=True)
            (d / "connector.json").write_text(json.dumps({
                "id": cid, "title": cid, "provides": {cap: "c.py"}, "usage": {cap: cap},
                "purpose": "", "can": [], "cannot": [], "data": ""}), encoding="utf-8")
            (d / "c.py").write_text("import json, os, sys\n" + code + "\n", encoding="utf-8")
        self.brief("profile", "connectors", json.dumps({"calendar": "fcal", "tasks": "ftask", "tg": "ftg"}))
        bindir = self.home / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        (bindir / "brief").write_text(f"#!/bin/bash\nexec {sys.executable} -I {self.repo / 'core/brief.py'} \"$@\"\n")
        (bindir / "brief").chmod(0o755)
        fake = self.tmp / "claude"
        fake.write_text("#!" + sys.executable + r"""
import json, os, subprocess, sys
if sys.argv[1:3] == ['auth', 'status']:
    print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai'})); sys.exit()
mode = os.environ['FAKE_MODE']
cnt = os.environ['REC'] + '.n'
n = int(open(cnt).read()) if os.path.exists(cnt) else 0
open(cnt, 'w').write(str(n + 1))
if mode == 'limit':
    print('Claude AI usage limit reached|1795430000'); sys.exit(1)
if mode == 'auth':
    print('Invalid API key · Please run /login'); sys.exit(1)
if mode == 'flaky' and n < 2:
    print('API Error: 529 Overloaded', file=sys.stderr); sys.exit(1)
if mode == 'partial':
    os.makedirs('.tmp', exist_ok=True)
    open('.tmp/m.txt', 'w').write('брифинг от Claude')
    subprocess.run(['brief', 'run', 'tg', 'notify', '.tmp/m.txt'], capture_output=True)
    print('Claude AI usage limit reached|1795430000'); sys.exit(1)
print('Готово от Claude')
""", encoding="utf-8")
        fake.chmod(0o755)
        self.brief("profile", "runtime", json.dumps({"kind": "claude"}))
        rec = self.tmp / "notify.txt"
        return rec, dict(self.env, CLAUDE_BIN=str(fake), FAKE_MODE=mode, REC=str(rec), BRIEF_RETRY_WAITS="1,1")

    def sent(self, rec):
        return [x.strip() for x in rec.read_text(encoding="utf-8").split("=====") if x.strip()] if rec.exists() else []

    def test_fallback_model(self):
        rec, env = self._fallback_env("limit")
        self.brief("profile", "fallback_runtime", json.dumps({"kind": "openai", "model": "fake-model",
                                                              "base_url": f"http://127.0.0.1:{self.srv.server_port}/v1"}))
        r = self.brief("agent", "тест", "force", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.ws / "Журнал/тест/последний.md").read_text(encoding="utf-8"), "Итог: всё сделано.")
        starts = [x["runtime"] for x in self.audit() if x["event"] == "start"]
        self.assertEqual(starts, ["claude", "openai"])  # основная упёрлась в лимит, сразу запасная
        self.assertEqual(self.sent(rec), [])
        self.assertIn("Запасная модель: fake-model", self.brief("doctor", env=env).stdout)

    def test_digest_without_model(self):
        rec, env = self._fallback_env("limit")
        (self.ws / "Агенты/утро.md").write_text("---\ntitle: Утренний брифинг\nfallback: digest\n---\nутро\n",
                                                encoding="utf-8")
        r = self.brief("agent", "утро", "force", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        msg = self.sent(rec)
        self.assertEqual(len(msg), 1)
        for x in ("Утренний брифинг без разбора", "кончился лимит подписки Claude", "10:00 Созвон с клубом",
                  "Смета Петрову (просрочена на 2 дн.)", "ждут ответа Петров", "не открытых чатов 2"):
            self.assertIn(x, msg[0])
        self.assertNotIn("Отменили", msg[0])
        self.assertNotIn("Потом", msg[0])

    def test_failure_told_once(self):
        rec, env = self._fallback_env("auth")
        for _ in range(2):
            self.brief("agent", "тест", "force", env=env)
        msg = self.sent(rec)
        self.assertEqual(len(msg), 1)  # частый агент не пишет одно и то же каждые 15 минут
        self.assertIn("Claude просит войти заново", msg[0])

    def test_retry_later(self):
        rec, env = self._fallback_env("flaky")
        r = self.brief("agent", "тест", env=env)  # по расписанию: с повторами
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.ws / "Журнал/тест/последний.md").read_text(encoding="utf-8"), "Готово от Claude")
        msg = self.sent(rec)
        self.assertEqual(len(msg), 1)
        self.assertIn("задерживается: Claude сейчас не отвечает. Повторю в", msg[0])

    def test_no_retry_after_message_sent(self):
        rec, env = self._fallback_env("partial")
        self.brief("agent", "тест", env=env)
        self.assertEqual(self.sent(rec), ["брифинг от Claude"])  # повтор прислал бы брифинг второй раз
        self.assertEqual(Path(str(rec) + ".n").read_text(), "1")

    def test_tick_runs_due_agents(self):
        sys.path.insert(0, str(self.repo / "core"))
        import schedule
        from datetime import datetime, timedelta
        mon9 = datetime(2026, 11, 23, 9, 0)  # понедельник
        self.assertTrue(schedule.due("0 9 * * 1-5", mon9))
        self.assertFalse(schedule.due("0 9 * * 1-5", mon9 + timedelta(days=5)))  # суббота
        self.assertTrue(schedule.due("*/15 9-20 * * *", mon9 + timedelta(minutes=45)))
        self.assertTrue(schedule.due("0 10 * * 0", datetime(2026, 11, 29, 10, 0)))  # воскресенье
        bindir = self.home / "bin"
        bindir.mkdir(parents=True)
        rec = self.tmp / "tick.txt"
        (bindir / "brief").write_text(f"#!/bin/bash\necho \"$@\" >> {rec}\n", encoding="utf-8")
        (bindir / "brief").chmod(0o755)
        (self.ws / "Журнал").mkdir(exist_ok=True)
        (self.ws / "Агенты/тест.md").write_text("---\ntitle: Тест\nschedule: \"* * * * *\"\n---\nтест\n", encoding="utf-8")
        (self.ws / "Агенты/редко.md").write_text("---\ntitle: Редко\nschedule: \"0 3 31 2 *\"\n---\nредко\n",
                                                 encoding="utf-8")
        self.assertNotIn("agent тест", rec.read_text() if rec.exists() else "")
        self.brief("profile", "agents", json.dumps({"тест": {"on": True}, "редко": {"on": True}}))  # по умолчанию выключены
        state = self.home / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "tick.json").write_text(json.dumps({"last": (datetime.now() - timedelta(days=3)).isoformat()}))
        r = self.brief("tick")
        self.assertEqual(r.returncode, 0, r.stderr)
        for _ in range(50):
            if rec.exists():
                break
            import time; time.sleep(0.1)
        import time; time.sleep(1)
        runs = rec.read_text().splitlines()
        self.assertEqual(runs.count("agent тест"), 1)  # пропущенное догоняется один раз
        self.assertNotIn("agent редко", runs)

    def test_codex_locked_to_brief_tools(self):
        fake = self.tmp / "codex"
        rec = self.tmp / "codex-call.json"
        fake.write_text("#!" + sys.executable + r"""
import json, subprocess, sys
a = sys.argv[1:]
if a[:2] == ["login", "status"]:
    sys.exit(0)
cfg = dict(x.split("=", 1) for x in [a[i + 1] for i, v in enumerate(a) if v == "-c"])
srv = subprocess.Popen([json.loads(cfg["mcp_servers.brief.command"]), *json.loads(cfg["mcp_servers.brief.args"])],
                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "write_file", "arguments": {"path": ".tmp/c.txt", "content": "секрет письма"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": "/etc/hosts"}}}]
out, _ = srv.communicate("".join(json.dumps(m) + "\n" for m in msgs), timeout=60)
prompt = sys.stdin.read()
json.dump({"argv": a, "replies": out.splitlines()}, open(%r, "w"), ensure_ascii=False)
open(a[a.index("-o") + 1], "w").write("Итог Codex")
print("user: " + prompt)
print("mcp: brief/write_file (completed)")
""" % str(rec), encoding="utf-8")
        fake.chmod(0o755)
        env = dict(self.env, CODEX_BIN=str(fake))
        self.brief("profile", "runtime", json.dumps({"kind": "codex"}))
        r = self.brief("agent", "тест", "force", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.ws / "Журнал/тест/последний.md").read_text(encoding="utf-8"), "Итог Codex")
        c = json.loads(rec.read_text(encoding="utf-8"))
        a = c["argv"]
        self.assertNotIn("danger-full-access", a)
        for x in ("--ignore-user-config", "shell_tool", "workspace-write", 'web_search="disabled"',
                  'mcp_servers.brief.default_tools_approval_mode="approve"'):
            self.assertIn(x, a)
        self.assertEqual((self.ws / ".tmp/c.txt").read_text(encoding="utf-8"), "секрет письма")
        self.assertIn("вне рабочей папки", c["replies"][2])
        log = next((self.ws / "Журнал/тест").glob("*.log")).read_text(encoding="utf-8")
        self.assertIn("mcp: brief/write_file", log)
        self.assertNotIn("Сделай тест", log)          # текст задания и писем в журнал не попадает
        self.assertNotIn("секрет письма", log)
        a = self.audit()
        self.assertTrue(any(x["event"] == "write" and x["agent"] == "тест" for x in a))
        self.assertEqual(oct(self.ws.stat().st_mode & 0o777), "0o700")

    def test_shared_bot_guard(self):
        py = Path.home() / ".brief/venv/bin/python"
        if not py.exists():
            self.skipTest("нет окружения с telethon")
        data = self.tmp / "tg"
        data.mkdir()
        old = self.tmp / "old-bot.json"
        old.write_text(json.dumps({"token": "T1", "chat_id": 5}))
        code = f"""
import json, sys
sys.argv = ["tg"]
sys.path.insert(0, {str(self.repo / 'connectors/telegram')!r})
import tg
from pathlib import Path
tg.OTHER_BOT_FILES = [Path({str(old)!r})]
sent = []
tg.bot_call = lambda token, method, p: sent.append((method, p)) or {{"ok": True, "result": {{"url": ""}}}}
r = [tg.bot_elsewhere("T1"), tg.bot_elsewhere("T2")]
store = {{"offset": 10}}
tg.warn_shared({{"token": "T2", "chat_id": 5}}, store, 10)
n1 = len([m for m in sent if m[0] == "sendMessage"])
tg.warn_shared({{"token": "T2", "chat_id": 5}}, store, 13)
tg.warn_shared({{"token": "T2", "chat_id": 5}}, store, 20)
n2 = len([m for m in sent if m[0] == "sendMessage"])
print(json.dumps([r, n1, n2]))
"""
        out = subprocess.run([str(py), "-I", "-c", code], capture_output=True, text=True,
                             env=dict(self.env, BRIEF_TG_DATA=str(data)), timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        busy, n1, n2 = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertIn("брифинг из", busy[0])
        self.assertEqual(busy[1], "")
        self.assertEqual((n1, n2), (0, 1))  # подряд без пропуска молчим, пропуск: одно предупреждение в день

    def test_wizard_token(self):
        import socket, time, urllib.request, urllib.error
        sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
        env = dict(self.env, BRIEF_WIZARD_TOKEN="секрет-теста".encode().hex())
        pr = subprocess.Popen([sys.executable, "-I", str(self.repo / "core/brief.py"), "setup", "--port", str(port), "--no-open"],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            def post(name, token):
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/{name}", data=b"{}", method="POST",
                                             headers={"X-Brief-Token": token, "Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        return r.status, json.loads(r.read())
                except urllib.error.HTTPError as e:
                    return e.code, {}
            for _ in range(50):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1); break
                except Exception:
                    time.sleep(0.2)
            self.assertEqual(post("state", "чужой".encode().hex())[0], 403)
            code, st = post("state", env["BRIEF_WIZARD_TOKEN"])
            self.assertEqual(code, 200)
            self.assertEqual(st["profile"]["name"], "Сергей")
            self.assertIn("fake", st["connectors"])
        finally:
            pr.terminate(); pr.wait()


if __name__ == "__main__":
    unittest.main()
