# -*- coding: utf-8 -*-
"""Проверка bitrix.py на локальном поддельном Битрикс24 (127.0.0.1, случайный порт).
Запуск из папки коннектора: python3 -m unittest test_bitrix
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "bitrix.py")
KEY = "s3cr3tKEYabc123xyz"
ME = 7


def iso(day, hour=19, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute).astimezone().isoformat(timespec="seconds")


def aware(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


class FakeBitrix(object):
    def __init__(self):
        self.scopes = {"task", "user"}
        self.users = [
            {"ID": "7", "NAME": "Дмитрий", "LAST_NAME": "Тестов", "WORK_POSITION": "Директор",
             "EMAIL": "dm@example.com", "PERSONAL_MOBILE": "+79990000007", "ACTIVE": True},
            {"ID": "21", "NAME": "Анна", "LAST_NAME": "Смирнова", "WORK_POSITION": "Менеджер",
             "EMAIL": "anna@example.com", "PERSONAL_MOBILE": "+79990000021", "ACTIVE": True},
            {"ID": "31", "NAME": "Иван", "LAST_NAME": "Петров", "WORK_POSITION": "Юрист",
             "EMAIL": "ip@example.com", "ACTIVE": True},
            {"ID": "32", "NAME": "Иван", "LAST_NAME": "Сидоров", "WORK_POSITION": "Бухгалтер",
             "EMAIL": "is@example.com", "ACTIVE": True},
            {"ID": "40", "NAME": "Ольга", "LAST_NAME": "Уволенная", "ACTIVE": False},
        ]
        self.tasks = {}
        self.next_id = 100
        self.calls = []
        self.fail_queue = []
        self.lock = threading.Lock()

    def user(self, uid):
        return next((u for u in self.users if int(u["ID"]) == int(uid)), None)

    def add_task(self, title, responsible=ME, creator=ME, deadline=None, status=2, tags=None,
                 description="", closed=None, priority=1):
        self.next_id += 1
        self.tasks[self.next_id] = {"id": self.next_id, "title": title, "description": description,
                                    "deadline": deadline, "status": status, "responsibleId": int(responsible),
                                    "createdBy": int(creator), "closedDate": closed, "tags": list(tags or []),
                                    "priority": priority}
        return self.next_id

    def view(self, t):
        u = self.user(t["responsibleId"])
        return {"id": str(t["id"]), "title": t["title"], "description": t["description"],
                "deadline": t["deadline"], "status": str(t["status"]), "responsibleId": str(t["responsibleId"]),
                "createdBy": str(t["createdBy"]), "closedDate": t["closedDate"], "priority": str(t["priority"]),
                "tags": [{"id": i + 1, "title": x} for i, x in enumerate(t["tags"])],
                "responsible": {"id": str(t["responsibleId"]),
                                "name": (u["NAME"] + " " + u["LAST_NAME"]) if u else ""}}

    @staticmethod
    def match(t, flt):
        fields = {"RESPONSIBLE_ID": "responsibleId", "CREATED_BY": "createdBy", "REAL_STATUS": "status",
                  "STATUS": "status", "CLOSED_DATE": "closedDate", "DEADLINE": "deadline", "ID": "id"}
        for k, v in flt.items():
            m = re.match(r"^(!|<=|>=|<|>)?([A-Z_]+)$", k)
            op, field = m.group(1) or "", fields[m.group(2)]
            cur = t[field]
            if field in ("closedDate", "deadline"):
                cur, v = aware(cur), aware(v)
                if cur is None:
                    return False
            elif isinstance(v, list):
                if (int(cur) in [int(x) for x in v]) == (op == "!"):
                    return False
                continue
            else:
                cur, v = int(cur), int(v)
            ok = {"": cur == v, "!": cur != v, "<": cur < v, ">": cur > v, "<=": cur <= v, ">=": cur >= v}[op]
            if not ok:
                return False
        return True

    def handle(self, method, params):
        if method.startswith("tasks.") and "task" not in self.scopes or \
                method.startswith("user.") and "user" not in self.scopes:
            return 401, {"error": "insufficient_scope",
                         "error_description": "The request requires higher privileges than provided by the webhook token"}
        if method == "profile":
            u = self.user(ME)
            return 200, {"result": {"ID": u["ID"], "NAME": u["NAME"], "LAST_NAME": u["LAST_NAME"], "ADMIN": False}}
        if method == "user.get":
            uid = params.get("ID") or (params.get("FILTER") or {}).get("ID")
            res = [u for u in self.users if uid is None or int(u["ID"]) == int(uid)]
            return 200, {"result": res, "total": len(res)}
        if method == "user.search":
            f = params.get("FILTER") or {}
            res = []
            for u in self.users:
                words = [str(u.get(x) or "").lower() for x in ("NAME", "LAST_NAME", "SECOND_NAME")]
                if "FIND" in f:
                    toks = str(f["FIND"]).lower().split()
                    if not all(any(w.startswith(t) for w in words) for t in toks):
                        continue
                if "NAME" in f and not words[0].startswith(str(f["NAME"]).lower()):
                    continue
                if "LAST_NAME" in f and not words[1].startswith(str(f["LAST_NAME"]).lower()):
                    continue
                res.append(u)
            return 200, {"result": res, "total": len(res)}
        if method == "tasks.task.list":
            found = [t for _, t in sorted(self.tasks.items()) if self.match(t, params.get("filter") or {})]
            start = int(params.get("start") or 0)
            page = found[start:start + 50]
            body = {"result": {"tasks": [self.view(t) for t in page]}, "total": len(found)}
            if start + 50 < len(found):
                body["next"] = start + 50
            return 200, body
        if method == "tasks.task.add":
            f = params.get("fields") or {}
            if not f.get("TITLE") or not f.get("RESPONSIBLE_ID"):
                return 400, {"error": "ERROR_CORE", "error_description": "Не указан ответственный"}
            tid = self.add_task(f["TITLE"], responsible=f["RESPONSIBLE_ID"], creator=ME, deadline=f.get("DEADLINE"),
                                status=2, tags=f.get("TAGS"), description=f.get("DESCRIPTION", ""),
                                priority=int(f.get("PRIORITY", 1)))
            return 200, {"result": {"task": self.view(self.tasks[tid])}}
        if method == "tasks.task.complete":
            t = self.tasks.get(int(params.get("taskId") or 0))
            if not t:
                return 400, {"error": "ERROR_CORE", "error_description": "Задача не найдена"}
            t["status"] = 5
            t["closedDate"] = datetime.now().astimezone().isoformat(timespec="seconds")
            return 200, {"result": {"task": self.view(t)}}
        return 404, {"error": "ERROR_METHOD_NOT_FOUND", "error_description": "Method not found!"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        fake = self.server.fake
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        m = re.match(r"^/rest/(\d+)/([^/]+)/([\w.]+)\.json$", self.path)
        with fake.lock:
            if not m or int(m.group(1)) != ME or m.group(2) != KEY:
                status, body = 401, {"error": "NO_AUTH_FOUND", "error_description": "Wrong authorization data"}
            else:
                method, params = m.group(3), json.loads(raw.decode("utf-8") or "{}")
                fake.calls.append((method, params))
                if fake.fail_queue:
                    status, body = fake.fail_queue.pop(0)
                else:
                    status, body = fake.handle(method, params)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class BitrixTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBitrix()
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.fake = self.fake
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.webhook = "http://127.0.0.1:%d/rest/%d/%s/" % (self.port, ME, KEY)
        self.home = tempfile.mkdtemp(prefix="brief-bitrix-test-")
        self.env = dict(os.environ, BRIEF_HOME=self.home, BRIEF_NO_GUI="1", BRIEF_BITRIX_PAUSE="0")
        self.today = date.today()
        self.stopped = False

    def tearDown(self):
        if not self.stopped:
            self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.home, ignore_errors=True)

    def run_cmd(self, *args, **kw):
        r = subprocess.run([sys.executable, SCRIPT] + [str(a) for a in args], input=kw.get("stdin", ""),
                           capture_output=True, text=True, env=self.env, timeout=60)
        for stream in (r.stdout, r.stderr):
            self.assertNotIn(KEY, stream, "ключ вебхука попал в вывод")
            self.assertNotIn("/rest/%d/" % ME, stream, "ссылка вебхука попала в вывод")
        self.assertNotIn(chr(0x2014), r.stdout)
        self.assertNotIn(chr(0x2192), r.stdout)
        try:
            data = json.loads(r.stdout)
        except ValueError:
            self.fail("не JSON: %r %r" % (r.stdout, r.stderr))
        return r.returncode, data

    def setup_ok(self):
        code, data = self.run_cmd("setup", stdin=self.webhook + "\n")
        self.assertEqual(code, 0, data)
        return data

    def write_input(self, obj):
        path = os.path.join(self.home, "in.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        return path

    def state(self, name):
        with open(os.path.join(self.home, "state", name), encoding="utf-8") as f:
            return json.load(f)

    # ---- setup ----

    def test_setup_ok(self):
        data = self.setup_ok()
        self.assertEqual(data, {"ok": True, "user": "Дмитрий Тестов", "portal": "127.0.0.1:%d" % self.port})
        path = os.path.join(self.home, "secrets", "bitrix.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["webhook"], self.webhook)
        self.assertEqual(saved["user_id"], ME)
        methods = [c[0] for c in self.fake.calls]
        self.assertEqual(methods, ["profile", "tasks.task.list", "user.get"])

    def test_setup_strips_method_from_pasted_url(self):
        code, data = self.run_cmd("setup", stdin=self.webhook + "profile.json\n")
        self.assertEqual(code, 0, data)

    def test_setup_bad_scope(self):
        self.fake.scopes = {"user"}
        code, data = self.run_cmd("setup", stdin=self.webhook + "\n")
        self.assertEqual(code, 1)
        self.assertIn("не хватает прав «Задачи»", data["error"])
        self.assertIn("Задачи и Пользователи", data["error"])
        self.assertFalse(os.path.exists(os.path.join(self.home, "secrets", "bitrix.json")))

    def test_setup_bad_key_and_bad_format(self):
        code, data = self.run_cmd("setup", stdin="http://127.0.0.1:%d/rest/%d/wrongkey99/\n" % (self.port, ME))
        self.assertEqual(code, 1)
        self.assertIn("не принял ключ", data["error"])
        code, data = self.run_cmd("setup", stdin="https://example.bitrix24.ru/marketplace/\n")
        self.assertEqual(code, 1)
        self.assertIn("не похоже на ссылку", data["error"])
        code, data = self.run_cmd("setup", stdin="http://example.bitrix24.ru/rest/7/abcdefgh12/\n")
        self.assertIn("https://", data["error"])

    def test_not_configured(self):
        code, data = self.run_cmd("list")
        self.assertEqual(code, 1)
        self.assertIn("не подключён", data["error"])
        code, data = self.run_cmd("doctor")
        self.assertEqual(code, 1)
        self.assertFalse(data["configured"])

    def test_doctor_ok(self):
        self.setup_ok()
        code, data = self.run_cmd("doctor")
        self.assertEqual(code, 0)
        self.assertTrue(data["ok"])
        self.assertTrue(data["webhook_works"])
        self.assertEqual(data["user"], "Дмитрий Тестов")

    # ---- list / done ----

    def test_list_overdue_and_delegated(self):
        self.setup_ok()
        t, f = self.today, self.fake
        overdue = f.add_task("Сдать отчёт", deadline=iso(t - timedelta(days=3)), tags=["Из созвонов"],
                             description="Подробности\nkey: call:1#d1", priority=2)
        soon = f.add_task("Позвонить юристу", deadline=iso(t + timedelta(days=1), 14, 30))
        f.add_task("Далёкая задача", deadline=iso(t + timedelta(days=30)))
        f.add_task("Без срока из переписки", tags=["Из переписки"])
        f.add_task("Без срока просто так")
        f.add_task("Уже сделано", deadline=iso(t), status=5, closed=iso(t, 10))
        f.add_task("Сделал, ждёт проверки начальника", creator=21, status=4, deadline=iso(t))
        late = f.add_task("Договор от Анны", responsible=21, deadline=iso(t - timedelta(days=2)))
        f.add_task("Проверить акт", responsible=31, status=4, deadline=iso(t + timedelta(days=2)))
        f.add_task("Чужая задача", responsible=21, creator=31, deadline=iso(t))
        code, data = self.run_cmd("list", 7)
        self.assertEqual(code, 0, data)
        self.assertEqual(data["today"], t.strftime("%Y-%m-%d"))
        titles = [d["title"] for d in data["due"]]
        self.assertEqual(titles, ["Сдать отчёт", "Позвонить юристу"])
        first = data["due"][0]
        self.assertEqual(first["id"], overdue)
        self.assertEqual(first["overdue_days"], 3)
        self.assertEqual(first["list"], "Из созвонов")
        self.assertEqual(first["notes"], "Подробности")
        self.assertEqual(first["priority"], "высокий")
        self.assertNotIn("time", first)
        self.assertEqual(first["url"], "http://127.0.0.1:%d/company/personal/user/7/tasks/task/view/%d/"
                         % (self.port, overdue))
        second = data["due"][1]
        self.assertEqual((second["id"], second["time"], second["list"]), (soon, "14:30", "Битрикс"))
        self.assertNotIn("overdue_days", second)
        self.assertEqual([d["title"] for d in data["agent_lists_undated"]], ["Без срока из переписки"])
        self.assertEqual(data["open_by_list"], {"Из созвонов": 1, "Битрикс": 3, "Из переписки": 1})
        dele = data["delegated"]
        self.assertEqual([d["title"] for d in dele], ["Договор от Анны", "Проверить акт"])
        self.assertEqual(dele[0]["id"], late)
        self.assertEqual(dele[0]["responsible"], "Анна Смирнова")
        self.assertEqual(dele[0]["overdue_days"], 2)
        self.assertEqual(dele[0]["status"], "ждёт выполнения")
        self.assertEqual(dele[1]["status"], "сделана, ждёт твоей проверки")
        self.assertNotIn("Чужая задача", json.dumps(data, ensure_ascii=False))

    def test_list_paginates(self):
        self.setup_ok()
        for i in range(120):
            self.fake.add_task("Задача %d" % i)
        code, data = self.run_cmd("list")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["open_by_list"], {"Битрикс": 120})
        starts = [c[1].get("start") for c in self.fake.calls if c[0] == "tasks.task.list"
                  and "RESPONSIBLE_ID" in c[1]["filter"] and "<REAL_STATUS" in c[1]["filter"]]
        self.assertEqual(starts, [0, 50, 100])

    def test_done(self):
        self.setup_ok()
        t = self.today
        f = self.fake
        f.add_task("Закрыта вчера", status=5, closed=iso(t - timedelta(days=1), 12))
        f.add_task("Закрыта давно", status=5, closed=iso(t - timedelta(days=40), 12))
        f.add_task("Открыта")
        code, data = self.run_cmd("done", 7)
        self.assertEqual(code, 0, data)
        self.assertEqual([d["title"] for d in data["completed"]], ["Закрыта вчера"])
        self.assertEqual(data["completed"][0]["completed"], (t - timedelta(days=1)).strftime("%Y-%m-%d"))

    # ---- add / proposals ----

    def test_add_self_and_dedupe(self):
        self.setup_ok()
        due = (self.today + timedelta(days=2)).strftime("%Y-%m-%d")
        inp = self.write_input({"items": [
            {"list": "Из созвонов", "title": "Отправить смету", "notes": "Созвон: стройка", "due": due,
             "key": "call:5#d1", "priority": "high"},
            {"list": "Из переписки", "title": "Ответить Анне", "notes": "", "due": due, "time": "10:15",
             "key": "tg:9#1", "assignee": "Дмитрий Тестов"},
        ]})
        code, data = self.run_cmd("add", inp)
        self.assertEqual(code, 0, data)
        self.assertEqual([c["title"] for c in data["created"]], ["Отправить смету", "Ответить Анне"])
        self.assertEqual(data["proposed"], [])
        tasks = list(self.fake.tasks.values())
        self.assertEqual(len(tasks), 2)
        a, b = tasks
        self.assertEqual(a["responsibleId"], ME)
        self.assertEqual(a["tags"], ["Из созвонов"])
        self.assertEqual(a["description"], "Созвон: стройка\nkey: call:5#d1")
        self.assertEqual(a["priority"], 2)
        dl = aware(a["deadline"])
        self.assertIsNotNone(dl.tzinfo)
        self.assertEqual((dl.strftime("%Y-%m-%d"), dl.hour, dl.minute), (due, 19, 0))
        self.assertEqual(aware(b["deadline"]).strftime("%H:%M"), "10:15")
        self.assertEqual(b["responsibleId"], ME)
        self.assertEqual(b["description"], "key: tg:9#1")
        keys = self.state("bitrix_keys.json")
        self.assertEqual(keys["call:5#d1"], a["id"])
        code, data = self.run_cmd("add", inp)
        self.assertEqual(code, 0, data)
        self.assertEqual(data["created"], [])
        self.assertEqual(data["skipped"], ["Отправить смету", "Ответить Анне"])
        self.assertEqual(len(self.fake.tasks), 2)

    def test_add_for_other_is_proposed_then_confirmed(self):
        self.setup_ok()
        due = (self.today + timedelta(days=3)).strftime("%Y-%m-%d")
        inp = self.write_input({"items": [
            {"list": "Из созвонов", "title": "Подготовить договор", "notes": "Созвон с юристом", "due": due,
             "key": "call:7#o1", "assignee": "Анна Смирнова"}]})
        code, data = self.run_cmd("add", inp)
        self.assertEqual(code, 0, data)
        self.assertEqual(data["created"], [])
        self.assertEqual(data["proposed"], [{"n": 1, "title": "Подготовить договор", "assignee": "Анна Смирнова",
                                             "due": due, "list": "Из созвонов"}])
        self.assertIn("note", data)
        self.assertEqual(self.fake.tasks, {}, "задача другому человеку поставлена без подтверждения")
        self.assertNotIn("tasks.task.add", [c[0] for c in self.fake.calls])
        props = self.state("bitrix_proposals.json")["items"]
        self.assertEqual(props[0]["user_id"], 21)
        code, data = self.run_cmd("add", inp)
        self.assertEqual(data["skipped"], ["Подготовить договор"])
        self.assertEqual(len(self.state("bitrix_proposals.json")["items"]), 1)
        code, data = self.run_cmd("proposals")
        self.assertEqual([p["n"] for p in data["proposals"]], [1])
        code, data = self.run_cmd("confirm", 1)
        self.assertEqual(code, 0, data)
        self.assertEqual(len(data["created"]), 1)
        self.assertEqual(data["created"][0]["responsible"], "Анна Смирнова")
        task = list(self.fake.tasks.values())[0]
        self.assertEqual(task["responsibleId"], 21)
        self.assertEqual(task["createdBy"], ME)
        self.assertEqual(task["description"], "Созвон с юристом\nkey: call:7#o1")
        self.assertEqual(self.state("bitrix_proposals.json")["items"], [])
        self.assertEqual(self.state("bitrix_keys.json")["call:7#o1"], task["id"])
        code, data = self.run_cmd("confirm", 1)
        self.assertEqual(data["not_found"], [1])
        code, data = self.run_cmd("add", inp)
        self.assertEqual(data["skipped"], ["Подготовить договор"])
        self.assertEqual(data["proposed"], [])
        self.assertEqual(len(self.fake.tasks), 1)

    def test_ambiguous_and_unknown_assignee_refused(self):
        self.setup_ok()
        inp = self.write_input({"items": [
            {"title": "Сверить счета", "due": self.today.strftime("%Y-%m-%d"), "key": "k1", "assignee": "Иван"},
            {"title": "Забрать пропуск", "key": "k2", "assignee": "Кузьма Неизвестный"},
            {"title": "Проверить акт", "key": "k3", "assignee": "Ольга Уволенная"},
        ]})
        code, data = self.run_cmd("add", inp)
        self.assertEqual(code, 0, data)
        prop = data["proposed"]
        self.assertEqual([p["n"] for p in prop], [1, 2, 3])
        self.assertIn("несколько", prop[0]["problem"])
        self.assertIn("Иван Петров", prop[0]["problem"])
        self.assertIn("не нашёл", prop[1]["problem"])
        self.assertIn("не нашёл", prop[2]["problem"])
        code, data = self.run_cmd("confirm", "1", "2")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["created"], [])
        self.assertEqual([r["n"] for r in data["refused"]], [1, 2])
        self.assertEqual(self.fake.tasks, {})
        code, data = self.run_cmd("reject", 1, 3, 9)
        self.assertEqual((data["rejected"], data["not_found"]), ([1, 3], [9]))
        self.assertEqual([p["n"] for p in self.state("bitrix_proposals.json")["items"]], [2])
        inp = self.write_input({"items": [{"title": "Новая", "key": "k4", "assignee": "Иван Петров"}]})
        code, data = self.run_cmd("add", inp)
        self.assertEqual(data["proposed"][0]["n"], 4, "номер предложения не должен повторяться")
        self.assertEqual(data["proposed"][0]["assignee"], "Иван Петров")

    def test_add_bad_input(self):
        self.setup_ok()
        inp = self.write_input({"items": [{"title": "Задача", "due": "25.11.2026"},
                                          {"title": "", "list": "Из созвонов"},
                                          {"title": "Брифинг готов", "list": "Брифинг", "key": "brief:1"}]})
        code, data = self.run_cmd("add", inp)
        self.assertEqual(code, 0)
        self.assertEqual(len(data["refused"]), 3)
        self.assertEqual(self.fake.tasks, {})
        code, data = self.run_cmd("add", os.path.join(self.home, "nope.json"))
        self.assertEqual(code, 1)

    # ---- complete / users ----

    def test_complete_by_title_and_id(self):
        self.setup_ok()
        f = self.fake
        a = f.add_task("Отправить смету", tags=["Из созвонов"])
        f.add_task("Дубль", tags=["Из созвонов"])
        f.add_task("Дубль", tags=["Из переписки"])
        dele = f.add_task("Договор от Анны", responsible=21)
        foreign = f.add_task("Чужая", responsible=21, creator=31)
        by_id = f.add_task("По номеру")
        inp = self.write_input({"items": [{"title": "  отправить   СМЕТУ "}, {"title": "Дубль"},
                                          {"title": "Чужая"}, {"title": "Договор от Анны"},
                                          {"title": "что угодно", "id": by_id}, {"title": "Дубль", "list": "Из переписки"},
                                          {"title": "x", "id": foreign}]})
        code, data = self.run_cmd("complete", inp)
        self.assertEqual(code, 0, data)
        self.assertEqual([c["title"] for c in data["completed"]],
                         ["Отправить смету", "Договор от Анны", "По номеру", "Дубль"])
        self.assertEqual(data["completed"][0]["id"], a)
        self.assertEqual(data["not_found"], ["Чужая", "x"])
        self.assertEqual(data["ambiguous"][0]["title"], "Дубль")
        self.assertEqual(sorted(data["ambiguous"][0]["lists"]), ["Из переписки", "Из созвонов"])
        self.assertEqual(f.tasks[a]["status"], 5)
        self.assertEqual(f.tasks[dele]["status"], 5)
        self.assertEqual(f.tasks[foreign]["status"], 2)

    def test_users_no_contacts(self):
        self.setup_ok()
        code, data = self.run_cmd("users", "Иван")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["users"], [{"id": 31, "name": "Иван Петров", "position": "Юрист"},
                                         {"id": 32, "name": "Иван Сидоров", "position": "Бухгалтер"}])
        text = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("@", text)
        self.assertNotIn("+7999", text)

    # ---- ошибки и секрет ----

    def test_rate_limit_retry(self):
        self.setup_ok()
        self.fake.fail_queue = [(503, {"error": "QUERY_LIMIT_EXCEEDED", "error_description": "Too many requests"})]
        code, data = self.run_cmd("list")
        self.assertEqual(code, 0, data)

    def test_error_description_with_secret_is_scrubbed(self):
        self.setup_ok()
        self.fake.fail_queue = [(400, {"error": "ERROR_CORE", "error_description": "bad call " + self.webhook})]
        code, data = self.run_cmd("list")
        self.assertEqual(code, 1)
        self.assertIn("***", data["error"])

    def test_network_down(self):
        self.setup_ok()
        self.server.shutdown()
        self.server.server_close()
        self.stopped = True
        code, data = self.run_cmd("list")
        self.assertEqual(code, 1)
        self.assertIn("Нет связи", data["error"])
        code, data = self.run_cmd("doctor")
        self.assertEqual(code, 1)
        self.assertFalse(data["webhook_works"])

    def test_no_long_dash_or_arrow_in_sources(self):
        for name in ("bitrix.py", "connector.json"):
            with open(os.path.join(HERE, name), encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn(chr(0x2014), text, name)
            self.assertNotIn(chr(0x2192), text, name)
        with open(os.path.join(HERE, "connector.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["provides"], {"tasks": "bitrix.py"})


if __name__ == "__main__":
    unittest.main()
