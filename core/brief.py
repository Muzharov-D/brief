# -*- coding: utf-8 -*-
"""brief: личный ассистент, который работает на этом компьютере.

Команды (все печатают понятный текст или JSON):
  brief setup                           мастер настройки в браузере (по шагам или экран «Доступы»)
  brief run <возможность> <аргументы>   вызвать коннектор: mail, draft, calendar, tasks, tg, voice, calls
  brief agent <агент> [force] [dry]     запустить агента сейчас (force: даже если нового нет;
                                        dry: пробно, ничего не отправляет и не меняет, отчёт в ~/.brief/state/dry)
  brief agents                          какие агенты есть, когда работают, включены ли
  brief connectors                      что можно подключить и что каждый коннектор может и не может
  brief profile [ключ [значение]]       настройки (имя, род, папка, модель); без аргументов показать
  brief audit [дней=1]                  журнал: что агенты читали и что сделали
  brief doctor                          проверка: что работает, что нет и что сделать
  brief schedule install|remove|list    расписание агентов в macOS
  brief update                          обновить ядро и агентов, свои настройки не трогает
Где что лежит:
  ядро и стандартные агенты: эта папка (git), обновляется командой brief update
  настройки, ключи, журнал аудита: ~/.brief (BRIEF_HOME), наружу не уходят
  ваши файлы (брифинги, протоколы, люди, свои агенты): папка из профиля, по умолчанию ~/Brief
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

CORE = Path(__file__).resolve().parent
ROOT = CORE.parent
HOME = Path(os.environ.get("BRIEF_HOME") or (Path.home() / ".brief"))
PROFILE = HOME / "profile.json"
AUDIT = HOME / "audit"
STATE = HOME / "state"
DATA = HOME / "data"
PLATFORM = "mac" if sys.platform == "darwin" else ("windows" if os.name == "nt" else "linux")
AGENT_LONG_CAPS = {"mail", "voice", "calls"}


# ---------- профиль и коннекторы ----------

def load_profile():
    try:
        return json.loads(PROFILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_profile(p):
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    PROFILE.write_text(json.dumps(p, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    PROFILE.chmod(0o600)


def workspace(p=None):
    if os.environ.get("BRIEF_DRY"):  # пробный прогон: агент работает в копии папки, настоящая не меняется
        return Path(os.environ["BRIEF_DRY"]) / "Brief"
    p = load_profile() if p is None else p
    return Path(os.path.expanduser(p.get("workspace") or "~/Brief"))


def venv_python():
    py = HOME / "venv" / ("Scripts/python.exe" if PLATFORM == "windows" else "bin/python")
    return str(py) if py.exists() else sys.executable


def connectors():
    out = {}
    for f in sorted((ROOT / "connectors").glob("*/connector.json")):
        try:
            out[f.parent.name] = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return out


def connector_env(p):
    env = dict(os.environ)
    env.update({
        "BRIEF_HOME": str(HOME),
        "BRIEF_WORKSPACE": str(workspace(p)),
        "BRIEF_TG_DATA": str(p.get("telegram_data") or (DATA / "telegram")),
        "BRIEF_TG_PY": str(ROOT / "connectors/telegram/tg.py"),
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def bound(cap, p):
    """Какие коннекторы подключены к возможности. Задачи можно вести сразу в нескольких системах: первая основная."""
    v = (p.get("connectors") or {}).get(cap)
    return [x for x in (v if isinstance(v, list) else [v]) if x]


def bound_pairs(p):
    return [(cap, cid) for cap in (p.get("connectors") or {}) for cid in bound(cap, p)]


def resolve(cap, p, cid=None):
    """Возможность (mail, tasks…) в команду запуска по профилю. Ошибка понятным текстом, если не подключено."""
    cid = cid or next(iter(bound(cap, p)), None)
    if not cid:
        raise LookupError(f"«{cap}» не подключено: brief connectors покажет, что можно подключить")
    spec = connectors().get(cid)
    if not spec or cap not in spec.get("provides", {}):
        raise LookupError(f"коннектор {cid} не умеет «{cap}»")
    if PLATFORM not in spec.get("platforms", [PLATFORM]):
        raise LookupError(f"коннектор {spec.get('title', cid)} не работает на этой системе")
    parts = spec["provides"][cap].split()
    script = ROOT / "connectors" / cid / parts[0]
    if script.suffix == ".py":
        argv = [venv_python(), "-I", str(script)]
    elif script.suffix == ".sh":
        argv = ["/bin/bash", str(script)]
    else:
        argv = [str(script)]
    return cid, spec, argv + parts[1:]


# ---------- журнал аудита ----------

def audit(event, **kw):
    """Одна строка JSON на действие: кто (агент), что вызвал, с какими аргументами, сколько получил. Содержимое не пишем."""
    AUDIT.mkdir(mode=0o700, parents=True, exist_ok=True)
    row = {"at": datetime.now().isoformat(timespec="seconds"), "agent": os.environ.get("BRIEF_AGENT") or os.environ.get("BRIEF_ACTOR") or "человек",
           "event": event, **kw}
    with open(AUDIT / f"{datetime.now():%Y-%m-%d}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def cmd_run(args, capture=False, timeout=None):
    """brief run <возможность> …: запуск коннектора с записью в журнал аудита."""
    if not args:
        sys.exit("brief run <возможность> <аргументы>; список: brief connectors")
    cap, rest = args[0], list(args[1:])
    p = load_profile()
    if cap == "date":
        out = datetime.now().astimezone().strftime("%A %d.%m.%Y %H:%M %Z")
        return (0, out) if capture else print(out)
    try:
        cids = bound(cap, p)
        if len(cids) > 1:
            code, out, err = run_many(cap, cids, rest, p, timeout)
        else:
            code, out, err = run_one(cap, None, rest, p, timeout)
    except LookupError as e:
        code, out, err = 1, json.dumps({"error": str(e)}, ensure_ascii=False), ""
    if capture:
        return code, out if out.strip() else (err.strip()[-2000:] or "")
    out = spill(cap, out)
    sys.stdout.write(out if out.endswith("\n") or not out else out + "\n")
    if err.strip() and code:
        sys.stderr.write(err)
    sys.exit(code)


SPILL = 25000  # длиннее Claude Code агенту не показывает, а прячет в свой файл вне рабочей папки


def spill(cap, out, limit=SPILL):
    """Длинный ответ коннектора агенту: целиком в файл .tmp рабочей папки, в ответе путь и начало.
    Так агент дочитывает его своим чтением файлов, а не теряет хвост (так было с длинным списком созвонов)."""
    if len(out) <= limit or not os.environ.get("BRIEF_AGENT"):
        return out
    try:
        out = json.dumps(json.loads(out), ensure_ascii=False, indent=1)  # по строкам: файл читается частями
    except ValueError:
        pass
    d = workspace() / ".tmp"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"ответ-{cap}-{datetime.now():%H%M%S}-{os.getpid()}.json"
    f.write_text(out, encoding="utf-8")
    return json.dumps({"ответ_в_файле": str(f), "символов": len(out), "строк": out.count("\n") + 1,
                       "как_читать": "ответ длинный и целиком лежит в этом файле внутри рабочей папки: прочитай его, "
                                     "при необходимости частями по строкам; команду повторять не нужно",
                       "начало": out[:1500]}, ensure_ascii=False, indent=1)


def run_one(cap, cid, rest, p, timeout):
    cid, spec, argv = resolve(cap, p, cid)
    if os.environ.get("BRIEF_AGENT") and rest and rest[0] in spec.get("setup_only", []):
        audit("отказ", cap=cap, args=rest[:3], why="команда настройки")
        return 1, json.dumps({"error": f"«{cap} {rest[0]}» делает только человек при настройке"}, ensure_ascii=False), ""
    if os.environ.get("BRIEF_DRY") and writes(spec, cap, rest):
        return dry_record(cap, cid, rest, p)
    t0 = time.time()
    try:
        r = subprocess.run(argv + rest, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=connector_env(p), cwd=str(workspace(p)) if workspace(p).exists() else None,
                           timeout=timeout)
        code, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        code, out, err = 124, json.dumps({"error": f"{cap}: не ответил за {timeout} с"}, ensure_ascii=False), ""
    audit("run", cap=cap, connector=cid, args=[a if len(a) < 80 else a[:77] + "…" for a in rest[:6]],
          code=code, bytes=len(out), ms=int((time.time() - t0) * 1000))
    return code, out, err


def writes(spec, cap, rest):
    """Команда что-то отправляет или меняет (connector.json, поле writes): в пробном прогоне её не выполняем."""
    w = (spec.get("writes") or {}).get(cap)
    return w is not None and ("*" in w or bool(rest) and rest[0] in w)


def dry_record(cap, cid, rest, p):
    """Пробный прогон: вместо отправки записываем, что агент сделал бы, вместе с содержимым файла."""
    row = {"cap": cap, "connector": cid, "args": [a[:200] for a in rest[:4]]}
    for a in rest:
        f = Path(a).expanduser()
        f = f if f.is_absolute() else workspace(p) / f
        if f.is_file():
            text = f.read_text(encoding="utf-8", errors="replace")[:20000]
            try:
                row["content"] = json.loads(text)
            except ValueError:
                row["content"] = text
            break
    with open(Path(os.environ["BRIEF_DRY"]) / "действия.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    audit("пробно", cap=cap, connector=cid, args=[a if len(a) < 80 else a[:77] + "…" for a in rest[:3]])
    return 0, json.dumps({"ok": True, "dry_run": "пробный прогон: не выполнено, записано в отчёт"}, ensure_ascii=False), ""


def run_many(cap, cids, rest, p, timeout):
    """Возможность подключена к нескольким системам (задачи в Напоминаниях и в Битрикс24).
    Чтение собирает ответы всех систем; запись идёт в основную или в ту, что указана в пункте полем "system"."""
    sub = rest[0] if rest else ""
    specs = {c: connectors().get(c, {}) for c in cids}

    def parsed(out):
        try:
            return json.loads(out)
        except ValueError:
            return out.strip()

    results, codes = {}, []
    if sub in ("add", "complete", "retime") and len(rest) > 1:
        src = Path(rest[1]).expanduser()
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return 1, json.dumps({"error": f"не прочитал {src.name}: {e}"}, ensure_ascii=False), ""
        items = data.get("items", []) if isinstance(data, dict) else data
        groups = {}
        for it in items:
            c = it.pop("system", None) if isinstance(it, dict) else None
            groups.setdefault(c if c in cids else cids[0], []).append(it)
        for c, its in groups.items():
            part = src.with_name(f".{c}-{src.name}")
            part.write_text(json.dumps({**data, "items": its} if isinstance(data, dict) else its, ensure_ascii=False),
                            encoding="utf-8")
            try:
                code, out, _ = run_one(cap, c, [sub, str(part), *rest[2:]], p, timeout)
            finally:
                part.unlink(missing_ok=True)
            results[c], codes = parsed(out), codes + [code]
    elif sub in ("", "list", "done"):
        for c in cids:
            code, out, _ = run_one(cap, c, rest, p, timeout)
            results[c], codes = parsed(out), codes + [code]
    else:  # команда, которую умеет одна из систем (proposals, confirm у Битрикс24)
        own = next((c for c in cids if re.search(rf"\b{re.escape(sub)}\b", specs[c].get("usage", {}).get(cap, ""))), cids[0])
        return run_one(cap, own, rest, p, timeout)
    names = ", ".join(f"{c} ({specs[c].get('title', c)})" for c in cids)
    results["как_устроено"] = (f"Задачи ведутся в нескольких системах: {names}. Новые идут в {cids[0]}. "
                               "Чтобы добавить, закрыть или перенести задачу в другой системе, укажи в пункте "
                               "\"system\": \"<ключ системы>\".")
    return (0 if all(c == 0 for c in codes) else 1), json.dumps(results, ensure_ascii=False, indent=1), ""


def cmd_audit(args):
    days = int(args[0]) if args else 1
    rows = []
    for i in range(days - 1, -1, -1):
        f = AUDIT / f"{(datetime.now() - timedelta(days=i)):%Y-%m-%d}.jsonl"
        if f.exists():
            rows += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not rows:
        print("За этот срок агенты ничего не вызывали.")
        return
    for r in rows:
        what = {"run": f"{r.get('cap')} {' '.join(r.get('args', []))}  ({r.get('bytes', 0)} байт, код {r.get('code')})",
                "read": f"прочитал файл {r.get('path')}", "write": f"записал файл {r.get('path')} ({r.get('bytes', 0)} байт)",
                "model": f"модель {r.get('model')}: отправлено {r.get('sent', 0)} симв., получено {r.get('got', 0)}",
                "start": f"старт, модель {r.get('runtime')} {r.get('model', '')}" + (", пробный прогон" if r.get("dry") else ""),
                "finish": ("закончил" if r.get("ok") else "закончил с ошибкой") + (" пробный прогон" if r.get("dry") else ""),
                "пробно": f"пробно, не выполнено: {r.get('cap')} {' '.join(r.get('args', []))}",
                "отказ": f"ОТКАЗАНО:{r.get('cap', '')} {' '.join(r.get('args', []))} {r.get('path', '')} ({r.get('why')})",
                }.get(r["event"], json.dumps(r, ensure_ascii=False))
        print(f"{r['at'][5:16].replace('T', ' ')}  {r['agent']:<16} {what}")


# ---------- агенты ----------

def agent_files(p):
    """Стандартные агенты из ядра; свои и изменённые лежат в <папка>/Агенты и имеют приоритет."""
    found = {f.stem: f for f in sorted((ROOT / "agents").glob("*.md"))}
    own = workspace(p) / "Агенты"
    if own.exists():
        found.update({f.stem: f for f in sorted(own.glob("*.md")) if not f.name.startswith("_")})
    return found


def header(md):
    m = re.match(r"---\n(.*?)\n---\n", md.read_text(encoding="utf-8"), re.S)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip().strip('"')
    return out


def agent_settings(name, md, p):
    h = header(md)
    a = (p.get("agents") or {}).get(name, {})
    return {"title": h.get("title", name), "schedule": a.get("schedule", h.get("schedule", "")),
            "on": a.get("on", False), "runtime": a.get("runtime") or p.get("runtime") or {"kind": "codex"},
            "source": "свой" if md.parent.name == "Агенты" else "стандартный", "description": h.get("description", ""),
            "fallback": h.get("fallback", "")}


def render(md, p):
    body = re.sub(r"\A---\n.*?\n---\n", "", md.read_text(encoding="utf-8"), count=1, flags=re.S)
    body = body.replace("{ИМЯ}", p.get("name") or "владелец компьютера").replace("{ПАПКА}", str(workspace(p)))
    body = body.replace("{ПОМОЩНИК}", p.get("assistant_name") or "Огонёк")
    if p.get("gender") == "f":
        pairs = json.loads((ROOT / "templates/род-ж.json").read_text(encoding="utf-8")) \
            if (ROOT / "templates/род-ж.json").exists() else []
        for a, b in pairs:
            body = body.replace(a, b)
    return body


def notify(text, p):
    f = STATE / f"notify-{os.getpid()}.txt"
    STATE.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    try:
        cmd_run(["tg", "notify", str(f)], capture=True, timeout=120)
    finally:
        f.unlink()


LOG_DAYS = 14


def private(ws):
    """Рабочая папка с брифингами, досье и журналами: открыта только владельцу компьютера."""
    for d in (ws, ws / "Журнал"):
        if d.exists() and d.stat().st_mode & 0o077:
            d.chmod(0o700)


def prune_logs(logroot):
    """Журналы агентов храним две недели, общий журнал запуска режем до последних 200 КБ."""
    cutoff = time.time() - LOG_DAYS * 86400
    for f in logroot.glob("*/*.log"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
    big = logroot / "launchd.log"
    if big.exists() and big.stat().st_size > 1_000_000:
        big.write_bytes(big.read_bytes()[-200_000:])


def cmd_agent(args):
    if not args:
        sys.exit("brief agent <агент> [force] [dry]; список: brief agents")
    name, dry = args[0], "dry" in args[1:]
    force = dry or "force" in args[1:]
    p = load_profile()
    files = agent_files(p)
    if name not in files:
        sys.exit(f"нет агента {name}; список: brief agents")
    s = agent_settings(name, files[name], p)
    if dry:
        drydir, snap = dry_start(name, workspace(p))
        logdir = drydir
    ws = workspace(p)
    if not dry:
        private(ws)
        logdir = ws / "Журнал" / name
        prune_logs(ws / "Журнал")
    logdir.mkdir(parents=True, exist_ok=True)
    log = logdir / f"{datetime.now():%Y-%m-%d}.log"

    def say(msg):
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%F %T}] {msg}\n")

    lock = STATE / "locks" / name
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < 3 * 3600:
            say("уже идёт, пропуск")
            if dry:
                dry_report(name, s, 1, "Этот агент сейчас уже работает. Попробуйте через несколько минут.", snap)
            return
    os.environ["BRIEF_AGENT"] = name
    try:
        sys.path.insert(0, str(CORE))
        import gate  # noqa: E402
        if not force:
            go, why = gate.check(name, lambda *a: cmd_run(list(a), capture=True, timeout=300))
            if not go:
                say("пропуск: " + why)
                return
            if why:
                say(why)
        prompt = render(files[name], p)
        chain = [s["runtime"]]
        fb = p.get("fallback_runtime")
        if fb and fb.get("kind") and runtime_name(fb) != runtime_name(s["runtime"]):
            chain.append(fb)  # запасная модель: только если владелец сам выбрал её в настройке
        frequent = runs_soon(s["schedule"], 60)  # следующий запуск по расписанию скоро: он и будет повтором
        waits = [0] if force or frequent else [0, *RETRY_WAITS]
        who = p.get("assistant_name") or "Огонёк"
        code, answer, fails, dead, used, partial = 1, "", [], set(), None, False
        for n, wait in enumerate(waits):
            if wait:
                at = datetime.now() + timedelta(seconds=wait)
                if n == 1:
                    notify(f"{who}: «{s['title']}» задерживается: {reasons(fails)}. Повторю в {at:%H:%M}.", p)
                say(f"повтор в {at:%H:%M}")
                time.sleep(wait)
            for rt in chain:
                if runtime_name(rt) in dead:
                    continue
                t0 = time.time()
                say(f"старт, модель: {rt.get('kind')} {rt.get('model', '')}".rstrip())
                audit("start", runtime=rt.get("kind"), model=rt.get("model", ""), **({"dry": True} if dry else {}))
                code, answer = run_model(rt, prompt, ws, log, p, say)
                if code == 0:
                    used = rt
                    break
                why = why_failed(answer)
                fails.append((rt, why))
                say(f"ошибка ({why[0]}): {(answer or '')[-300:]}")
                audit("finish", ok=False, why=why[0], runtime=rt.get("kind"))
                if not dry and sent_since(name, t0):  # сообщение уже ушло: повтор прислал бы его второй раз
                    say("сообщение владельцу уже ушло, повтор не нужен")
                    code, partial, used = 0, True, rt
                    break
                if why[0] in ("auth", "setup"):
                    dead.add(runtime_name(rt))  # без человека не починится: повторять незачем
            if code == 0 or len(dead) == len(chain):
                break
        (logdir / "последний.md").write_text(answer or "", encoding="utf-8")
        if dry:
            say("пробный прогон " + ("готов" if code == 0 else f"с ошибкой: {answer[-500:] if answer else code}"))
            audit("finish", ok=code == 0, dry=True)
            dry_report(name, s, code, answer if code == 0 or not fails else reasons(fails) + "\n\n" + (answer or ""), snap)
        elif code == 0:
            gate.commit(name)
            failures(name, None)
            say("готово" + (f" через запасную модель {runtime_name(used)}" if used is not chain[0] else "")
                + (", после ошибки модели" if partial else ""))
            audit("finish", ok=True, runtime=used.get("kind"))
        else:
            text = reasons(fails)
            say(f"не получилось: {text}")
            if s.get("fallback") == "digest":
                notify(digest(p, s, text), p)
                say("отправлена сводка без модели")
            elif failures(name, sorted({w[0] for _, w in fails})):
                notify(f"{who}: «{s['title']}» не отработал: {text}."
                       + (" Попробую на следующем запуске." if frequent else ""), p)
    finally:
        if dry:  # упал до отчёта: копию личных файлов всё равно убираем
            shutil.rmtree(Path(os.environ["BRIEF_DRY"]) / "Brief", ignore_errors=True)
        try:
            lock.rmdir()
        except OSError:
            pass


# повтор через 20 минут и ещё через 40 (BRIEF_RETRY_WAITS только для тестов)
RETRY_WAITS = tuple(int(x) for x in os.environ.get("BRIEF_RETRY_WAITS", "1200,2400").split(",") if x)


def run_model(rt, prompt, ws, log, p, say):
    if rt.get("kind") == "codex":
        return run_codex(prompt, ws, log)
    if rt.get("kind") == "claude":
        return run_claude(prompt, ws, log, rt)
    if rt.get("kind") in ("openai", "ollama", "local"):
        sys.path.insert(0, str(CORE))
        import harness  # noqa: E402
        return harness.run(prompt, rt, p, ws, say, lambda a: cmd_run(a, capture=True, timeout=600), audit)
    return 1, f"неизвестная модель в профиле: {rt}"


def runtime_name(rt):
    k = (rt or {}).get("kind")
    return {"claude": "Claude", "codex": "ChatGPT"}.get(k) or (rt or {}).get("model") or "модель"


def runs_soon(cron, minutes):
    if not cron:
        return False
    sys.path.insert(0, str(CORE))
    import schedule  # noqa: E402
    now = datetime.now().replace(second=0, microsecond=0)
    return any(schedule.due(cron, now + timedelta(minutes=i)) for i in range(1, minutes + 1))


def why_failed(text):
    """Почему модель не отработала, простыми словами: (вид, подробность). Виды: limit, auth, setup, down, other."""
    t = text or ""
    low = t.lower()
    if re.search(r"не найден", low):
        return "setup", ""
    if re.search(r"usage limit|rate.?limit|limit reached|hit your limit|quota|\b429\b|лимит", low):
        when = ""
        m = re.search(r"limit reached\|(\d{10})", t)
        if m:
            when = "снова можно в " + datetime.fromtimestamp(int(m.group(1))).strftime("%H:%M")
        elif (m := re.search(r"resets? (?:at )?([0-9][0-9:]*\s*(?:am|pm)?)", low)):
            when = "сброс в " + m.group(1).strip()
        elif (m := re.search(r"try again in ([^.\n]+)", low)):
            when = "снова можно через " + re.sub(r"hours?", "ч", re.sub(r"minutes?", "мин", m.group(1))).strip()
        return "limit", when
    if re.search(r"log ?in|logged|unauthori[sz]ed|\b401\b|\b403\b|invalid api key|authenticat|войти|войдите|вход в", low):
        return "auth", ""
    if re.search(r"overloaded|\b5\d\d\b|connection|timed? ?out|network|unavailable|не ответил|не уложил|время", low):
        return "down", ""
    line = next((x.strip() for x in t.splitlines() if x.strip()), "")
    return "other", line[:150]


def reasons(fails):
    """Причины без повторов: «кончился лимит подписки Claude (сброс в 15:00); ChatGPT сейчас не отвечает»."""
    out = []
    for rt, (kind, detail) in fails:
        who = runtime_name(rt)
        text = {"limit": f"кончился лимит подписки {who}" + (f" ({detail})" if detail else ""),
                "auth": f"{who} просит войти заново: откройте Огонёк, шаг «Где думает помощник»",
                "setup": f"{who} не найден на компьютере: откройте Огонёк, шаг «Где думает помощник»",
                "down": f"{who} сейчас не отвечает"}.get(kind, f"{who} вернул ошибку: {detail}")
        if text not in out:
            out.append(text)
    return "; ".join(out) or "модель не ответила"


def sent_since(agent, t0):
    """Агент успел отправить владельцу сообщение в бота после t0 (по журналу аудита)."""
    f = AUDIT / f"{datetime.now():%Y-%m-%d}.jsonl"
    if not f.exists():
        return False
    for x in f.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(x)
        except ValueError:
            continue
        if r.get("event") == "run" and r.get("agent") == agent and r.get("cap") == "tg" and r.get("code") == 0 \
                and (r.get("args") or [""])[0] == "notify" and datetime.fromisoformat(r["at"]).timestamp() >= t0 - 1:
            return True
    return False


def failures(agent, kinds):
    """Помнит последнюю неудачу агента, чтобы частые агенты (бот, встречи) не писали одно и то же каждые 15 минут.
    kinds=None: агент отработал, забыть. Возвращает True, если о неудаче стоит написать владельцу."""
    f = STATE / "failures.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if kinds is None:
        if data.pop(agent, None) is not None:
            f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return False
    prev = data.get(agent) or {}
    tell = prev.get("kinds") != kinds or time.time() - prev.get("at", 0) > 6 * 3600
    if tell:
        data[agent] = {"kinds": kinds, "at": time.time()}
        STATE.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return tell


def digest(p, s, reason):
    """Сводка без модели, когда ни одна модель не ответила: встречи, задачи на сегодня, сколько писем и чатов ждёт.
    Без разбора и без текста писем, только то, что можно собрать кодом."""
    from html import escape
    today = datetime.now().date()

    def get(*a):
        cap = a[0]
        if cap not in (p.get("connectors") or {}) or not bound(cap, p):
            return None, None
        code, out = cmd_run(list(a), capture=True, timeout=600)
        try:
            d = json.loads(out)
        except ValueError:
            return None, "не прочиталось"
        return (None, "не прочиталось") if code or (isinstance(d, dict) and d.get("error")) else (d, None)

    who = p.get("assistant_name") or "Огонёк"
    lines = [f"<b>{escape(s['title'])} без разбора</b>",
             f"{escape(who)}: модель не ответила ({escape(reason)}). Ниже то, что собрано без неё.", ""]
    cal, err = get("calendar", "24")
    if cal is not None or err:
        lines.append("<b>Встречи сегодня</b>")
        evs = []
        for e in (cal or {}).get("events", []):
            try:
                st = datetime.fromisoformat(str(e.get("start", "")).replace("Z", "+00:00")).astimezone()
            except ValueError:
                continue
            if st.date() == today and not e.get("canceled"):
                evs.append(f"{st:%H:%M} {escape(e.get('title') or 'без названия')}")
        lines += evs or [err or "нет"]
        lines.append("")
    tasks, err = get("tasks", "list", "1")
    if tasks is not None or err:
        lines.append("<b>Задачи на сегодня</b>")
        parts = [tasks] if isinstance(tasks, dict) and "due" in tasks else \
            [v for v in (tasks or {}).values() if isinstance(v, dict)]
        items = []
        for part in parts:
            for t in part.get("due", []):
                if str(t.get("due", ""))[:10] <= today.isoformat():
                    late = f" (просрочена на {t['overdue_days']} дн.)" if t.get("overdue_days") else ""
                    items.append(f"- {escape(t.get('title') or '')}{late}")
        lines += items[:15] or [err or "нет"]
        lines.append("")
    mail, err = get("mail", "14", "100")
    if mail is not None or err:
        if isinstance(mail, list):
            unread = sum(1 for m in mail if not m.get("read"))
            flagged = sum(1 for m in mail if m.get("flagged"))
            lines.append(f"<b>Почта</b>: за ночь {len(mail)} писем, непрочитанных {unread}"
                         + (f", с флажком {flagged}" if flagged else ""))
        else:
            lines.append(f"<b>Почта</b>: {err}")
    tg, err = get("tg", "inbox", "16")
    if tg is not None or err:
        if isinstance(tg, dict):
            names = [escape(c.get("chat") or "") for c in tg.get("awaiting_reply", [])]
            lines.append("<b>Telegram</b>: " + (f"ждут ответа {', '.join(names[:10])}" if names else "ответа никто не ждёт")
                         + (f"; не открытых чатов {tg['unread_not_opened']}" if tg.get("unread_not_opened") else ""))
        else:
            lines.append(f"<b>Telegram</b>: {err}")
    return "\n".join(lines).strip()


DRY_SKIP = ("Журнал", ".tmp")


def dry_start(name, real):
    """Копия рабочей папки для пробного прогона: агент пишет протоколы и досье в неё, настоящие не трогает,
    иначе пробный протокол созвона пометил бы созвон обработанным."""
    d = STATE / "dry" / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(mode=0o700, parents=True)
    (STATE / "dry").chmod(0o700)
    big = lambda folder, names: [n for n in names if (Path(folder) / n).is_file()  # noqa: E731
                                 and (Path(folder) / n).stat().st_size > 5_000_000]
    if real.exists():
        shutil.copytree(real, d / "Brief", symlinks=True, ignore=lambda folder, names: [
            n for n in names if Path(folder) == real and n in DRY_SKIP] + big(folder, names))
    for sub in DRY_SKIP:
        (d / "Brief" / sub).mkdir(parents=True, exist_ok=True)
    os.environ["BRIEF_DRY"] = str(d)
    return d, {str(f.relative_to(d / "Brief")): f.stat().st_mtime for f in (d / "Brief").rglob("*") if f.is_file()}


def dry_report(name, s, code, answer, snap):
    """Отчёт пробного прогона: что пришло бы в бота, какие задачи и черновики появились бы, какие файлы записал.
    Копию папки после этого удаляем: второй экземпляр личных файлов не держим."""
    d = Path(os.environ["BRIEF_DRY"])
    acts = d / "действия.jsonl"
    actions = [json.loads(x) for x in acts.read_text(encoding="utf-8").splitlines() if x.strip()] if acts.exists() else []
    root, files = d / "Brief", []
    for f in sorted(root.rglob("*")):
        rel = f.relative_to(root)
        if not f.is_file() or rel.parts[0] in DRY_SKIP or f.name.startswith("."):
            continue
        if str(rel) not in snap or f.stat().st_mtime > snap[str(rel)]:
            text = f.read_text(encoding="utf-8", errors="replace")[:8000] if f.suffix in (".md", ".txt") else ""
            files.append({"path": str(rel), "new": str(rel) not in snap, "text": text})
    rep = {"agent": name, "title": s["title"], "at": datetime.now().isoformat(timespec="seconds"), "ok": code == 0,
           "answer": (answer or "")[-6000:], "actions": actions, "files": files}
    (d / "отчёт.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    acts.unlink(missing_ok=True)
    shutil.rmtree(root, ignore_errors=True)
    return rep


def dry_last(name):
    f = STATE / "dry" / name / "отчёт.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


CODEX_OFF = ["shell_tool", "computer_use", "in_app_browser", "browser_use", "browser_use_external",
             "image_generation", "apps", "plugins"]


def service_lines(text):
    """В журнал агента только служебные строки (ошибки, вызовы инструментов), без текста писем и ответов модели."""
    keep = re.compile(r"^(mcp:|error|ERROR|warning|WARN|Error|Traceback|\s*File \"|[A-Za-z]+Error:)")
    return "".join(x + "\n" for x in text.splitlines() if keep.match(x))[-4000:]


def run_codex(prompt, ws, log):
    """Подписка ChatGPT через Codex без окна. Командной строки, интернета, браузера и плагинов у Codex нет:
    читать, писать и звать коннекторы он может только инструментами brief (core/mcp_brief.py), внутри рабочей
    папки и с записью в журнал аудита. Свой config.toml пользователя не подключается."""
    r = subprocess.run([str(CORE / "codex-path")], capture_output=True, text=True)
    codex = r.stdout.strip()
    if r.returncode or not codex:
        return 1, "не найден Codex: обновите ChatGPT или запустите установку снова"
    hint = ("Работай только инструментами сервера brief: brief (команды из задания, то, что стоит после `run`), "
            "read_file, write_file, list_files. Других инструментов нет. Файлы только внутри рабочей папки, "
            "временные в .tmp/. Команды почты, голосовых и созвонов отвечают до 10 минут.\n")
    out = ws / "Журнал" / ".codex-last.md"
    out.unlink(missing_ok=True)
    agent = os.environ.get("BRIEF_AGENT", "codex")
    q = lambda v: json.dumps(str(v), ensure_ascii=False)  # noqa: E731  строка TOML
    mcp_env = "{" + ", ".join(f"{k}={q(v)}" for k, v in (("BRIEF_HOME", HOME), ("PATH", os.environ.get("PATH", "")),
                                                            ("LANG", "ru_RU.UTF-8"))
                                     + ((("BRIEF_DRY", os.environ["BRIEF_DRY"]),) if os.environ.get("BRIEF_DRY") else ())) + "}"
    cmd = [codex, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
           "-s", "workspace-write", *[x for o in CODEX_OFF for x in ("--disable", o)],
           "-c", 'web_search="disabled"', "-c", 'approval_policy="never"', "-c", 'model_reasoning_effort="medium"',
           "-c", "sandbox_workspace_write.network_access=false",
           "-c", f"mcp_servers.brief.command={q(sys.executable)}",
           "-c", "mcp_servers.brief.args=[" + ", ".join(q(x) for x in ("-I", CORE / "mcp_brief.py", agent, ws)) + "]",
           "-c", f"mcp_servers.brief.env={mcp_env}",
           "-c", "mcp_servers.brief.tool_timeout_sec=900", "-c", 'mcp_servers.brief.default_tools_approval_mode="approve"',
           "-C", str(ws), "-o", str(out), "-"]
    try:
        r = subprocess.run(cmd, input=hint + prompt, text=True, capture_output=True, timeout=2400)
        rc, err = r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        rc, err = 124, "error: Codex не уложился в 40 минут"
    with open(log, "a", encoding="utf-8") as lf:
        lf.write(service_lines(err))
    answer = out.read_text(encoding="utf-8") if out.exists() else ""
    out.unlink(missing_ok=True)
    return rc, answer or service_lines(err)[-1000:]


def claude_bin():
    r = subprocess.run([str(CORE / "claude-path")], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def claude_status():
    """Claude Code и вход по подписке Claude (Pro, Max): тот же аккаунт, что и чат claude.ai."""
    exe = claude_bin()
    if not exe:
        return {"found": False}
    try:
        r = subprocess.run([exe, "auth", "status", "--json"], capture_output=True, text=True, timeout=60)
        st = json.loads(r.stdout or "{}")
    except (subprocess.TimeoutExpired, ValueError):
        st = {}
    return {"found": True, "path": exe, "logged_in": bool(st.get("loggedIn")),
            "method": st.get("authMethod", ""), "plan": st.get("subscriptionType") or ""}


def run_claude(prompt, ws, log, rt):
    """Подписка Claude через Claude Code без окна. Свои настройки, навыки, хуки и MCP пользователя не подключаются
    (--safe-mode); инструменты: только `brief run …` и файлы внутри рабочей папки; интернета нет."""
    exe = claude_bin()
    if not exe:
        return 1, "не найден Claude Code: откройте настройку (brief setup) и войдите в Claude"
    hint = ("Команды `brief run …` для почты, голосовых и созвонов работают до 10 минут: ставь таймаут "
            "команды 600000 мс и дожидайся результата, не запускай их повторно параллельно. Вызывай `brief run …` "
            "отдельной командой, без конвейеров и `&&`: другие команды запрещены. Файлы читай и пиши только "
            "внутри рабочей папки; временные файлы клади в .tmp.\n")
    area = f"/{Path(ws).resolve()}/**"  # настоящий путь: Claude сверяет правила по нему
    allowed = [f"Bash(brief run *)", f"Read({area})", f"Write({area})", f"Edit({area})", "Glob", "Grep"]
    env = dict(os.environ, PATH=f"{HOME / 'bin'}:{os.environ.get('PATH', '')}")
    env.pop("ANTHROPIC_API_KEY", None)  # только подписка: случайный ключ API из окружения не должен списывать деньги
    with open(log, "a", encoding="utf-8") as lf:
        try:
            r = subprocess.run([exe, "-p", "--safe-mode", "--no-session-persistence",
                                "--model", rt.get("model") or "sonnet",
                                "--tools", "Bash,Read,Write,Edit,Glob,Grep",
                                "--allowedTools", *allowed,
                                "--permission-mode", "dontAsk", "--permission-prompts", "none",
                                "--output-format", "text"],
                               input=hint + prompt, text=True, capture_output=True, cwd=str(ws), env=env,
                               timeout=int(rt.get("time_limit", 2400)))
        except subprocess.TimeoutExpired:
            lf.write("Claude: превышено время\n")
            return 124, "Claude не уложился в отведённое время"
        lf.write(service_lines(r.stderr))
    if r.returncode:  # причина (лимит, вход, сбой) бывает и в stdout, и в stderr
        return r.returncode, (r.stdout.strip() + "\n" + r.stderr.strip()[-1500:]).strip() or "Claude не ответил"
    return 0, r.stdout.strip() or "Claude не ответил"


def cmd_agents(_args):
    p = load_profile()
    for name, md in agent_files(p).items():
        s = agent_settings(name, md, p)
        rt = s["runtime"]
        print(f"{'вкл ' if s['on'] else 'выкл'}  {name:<18} {s['title']:<26} {s['schedule']:<16} "
              f"{rt.get('kind')}{' ' + rt['model'] if rt.get('model') else ''}  ({s['source']})")


def cmd_connectors(_args):
    p = load_profile()
    used = {cid for _, cid in bound_pairs(p)}
    for cid, c in connectors().items():
        if PLATFORM not in c.get("platforms", [PLATFORM]):
            continue
        print(f"\n{'[подключено] ' if cid in used else ''}{c.get('title', cid)}  ({cid})")
        print(f"  Зачем: {c.get('purpose', '')}")
        for x in c.get("can", []):
            print(f"  Может: {x}")
        for x in c.get("cannot", []):
            print(f"  Не может: {x}")
        print(f"  Данные: {c.get('data', '')}")


def cmd_profile(args):
    p = load_profile()
    if not args:
        print(json.dumps(p, ensure_ascii=False, indent=2))
        return
    if len(args) == 1:
        print(json.dumps(p.get(args[0]), ensure_ascii=False))
        return
    key, val = args[0], " ".join(args[1:])
    try:
        val = json.loads(val)
    except ValueError:
        pass
    p[key] = val
    save_profile(p)
    print(f"{key} сохранено")


def model_row(rt, label):
    if rt.get("kind") == "codex":
        r = subprocess.run([str(CORE / "codex-path")], capture_output=True, text=True)
        ok = r.returncode == 0
        if ok:
            ok = subprocess.run([r.stdout.strip(), "login", "status"], capture_output=True).returncode == 0
        return (f"{label}: Codex (ChatGPT)", ok, "", "войдите: codex login")
    if rt.get("kind") == "claude":
        st = claude_status()
        return (f"{label}: подписка Claude", st.get("logged_in", False), st.get("plan", ""),
                "откройте brief setup и войдите в Claude" if st.get("found") else "откройте brief setup: поставлю Claude Code")
    sys.path.insert(0, str(CORE))
    import harness  # noqa: E402
    ok, detail = harness.ping(rt)
    return (f"{label}: {rt.get('model', '')}", ok, detail, "запустите программу с моделью (Ollama, LM Studio)")


def cmd_doctor(_args):
    p = load_profile()
    rows = [("Имя", bool(p.get("name")), p.get("name", ""), "brief profile name <Имя>")]
    ws = workspace(p)
    rows.append(("Папка", ws.exists(), str(ws), "запустите установку"))
    rows.append(model_row(p.get("runtime") or {"kind": "codex"}, "Модель"))
    if (p.get("fallback_runtime") or {}).get("kind"):
        rows.append(model_row(p["fallback_runtime"], "Запасная модель"))
    for cap, cid in bound_pairs(p):
        spec = connectors().get(cid, {})
        chk = spec.get("check")
        if not chk or chk[0] != cap:
            continue
        try:
            code, out, err = run_one(cap, cid, chk[1:], p, 180)
            out = out if out.strip() else err[-500:]
        except LookupError as e:
            code, out = 1, json.dumps({"error": str(e)}, ensure_ascii=False)
        try:
            err = json.loads(out).get("error")
        except (ValueError, AttributeError):
            err = None if code == 0 else out[:120]
        rows.append((spec.get("title", cid), code == 0 and not err, str(err or "")[:120], spec.get("setup", "")[:160]))
    for name, ok, detail, fix in rows:
        print(f"{'ок ' if ok else 'НЕТ'}  {name}{('  ' + detail) if detail else ''}")
        if not ok and fix:
            print(f"     Что сделать: {fix}")


def cmd_schedule(args):
    sys.path.insert(0, str(CORE))
    import schedule  # noqa: E402
    p = load_profile()
    jobs = {n: agent_settings(n, f, p) for n, f in agent_files(p).items()}
    app = HOME / "helpers" / "Огонёк.app" / "Contents" / "MacOS" / "ogonyok"
    res = schedule.apply(args[0] if args else "list", jobs, HOME / "bin" / "brief", workspace(p) / "Журнал", app)
    print(json.dumps(res, ensure_ascii=False, indent=2))


CATCH_UP = 6 * 3600  # пропущенное, пока Mac спал, догоняем не дальше чем за 6 часов


def cmd_tick(_args):
    """Будит launchd: запустить агентов, которым пора с прошлого тика. Каждый агент в своём процессе:
    долгий утренний брифинг не задерживает ответы бота."""
    sys.path.insert(0, str(CORE))
    import schedule  # noqa: E402
    p = load_profile()
    st = HOME / "state" / "tick.json"
    st.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().replace(second=0, microsecond=0)
    try:
        last = datetime.fromisoformat(json.loads(st.read_text(encoding="utf-8"))["last"])
    except (OSError, ValueError, KeyError):
        last = now - timedelta(minutes=1)
    last = max(last, now - timedelta(seconds=CATCH_UP))
    minutes = [last + timedelta(minutes=i) for i in range(1, int((now - last).total_seconds() // 60) + 1)]
    st.write_text(json.dumps({"last": now.isoformat()}), encoding="utf-8")
    started = []
    for name, f in agent_files(p).items():
        s = agent_settings(name, f, p)
        if s["on"] and s["schedule"] and any(schedule.due(s["schedule"], m) for m in minutes):
            log = open(workspace(p) / "Журнал" / "launchd.log", "a", encoding="utf-8")
            subprocess.Popen([str(HOME / "bin" / "brief"), "agent", name], stdout=log, stderr=log,
                             stdin=subprocess.DEVNULL, start_new_session=True)
            started.append(name)
    if started:
        print(f"[{now:%Y-%m-%d %H:%M}] запущены: {', '.join(started)}")


REPO = "Muzharov-D/brief"  # открытый репозиторий: обновление без git и без входа в GitHub


def update_archive():
    """Установка без git (скачана архивом): берём последнюю версию с GitHub архивом и кладём поверх кода.
    Настройки и файлы пользователя лежат в ~/.brief и ~/Brief и не трогаются."""
    import tarfile
    import tempfile
    import urllib.request
    ver = ROOT / ".version"
    try:
        with urllib.request.urlopen(f"https://api.github.com/repos/{REPO}/commits/main", timeout=30) as r:
            sha = json.load(r)["sha"]
    except (OSError, ValueError, KeyError) as e:
        sys.exit(f"Обновление не скачалось: {e}")
    if ver.exists() and ver.read_text().strip() == sha:
        print("Уже последняя версия.")
        return False
    old_news = set((ROOT / "ЧТО-НОВОГО.md").read_text(encoding="utf-8").splitlines()) \
        if (ROOT / "ЧТО-НОВОГО.md").exists() else set()
    with tempfile.TemporaryDirectory() as tmp:
        arc = Path(tmp) / "brief.tar.gz"
        try:
            urllib.request.urlretrieve(f"https://codeload.github.com/{REPO}/tar.gz/{sha}", arc)
            with tarfile.open(arc) as t:
                t.extractall(tmp, filter="data")
        except (OSError, tarfile.TarError) as e:
            sys.exit(f"Обновление не скачалось: {e}")
        new = next(p for p in Path(tmp).iterdir() if p.is_dir())
        keep = {str(f.relative_to(new)) for f in new.rglob("*")}
        for f in sorted(ROOT.rglob("*"), reverse=True):  # файлы, которых в новой версии нет
            rel = str(f.relative_to(ROOT))
            if rel not in keep and rel != ".version" and not rel.startswith("."):
                f.unlink() if f.is_file() or f.is_symlink() else shutil.rmtree(f, ignore_errors=True)
        shutil.copytree(new, ROOT, dirs_exist_ok=True)
    ver.write_text(sha + "\n")
    news = [x for x in (ROOT / "ЧТО-НОВОГО.md").read_text(encoding="utf-8").splitlines()
            if x.startswith("- ") and x not in old_news]
    print("Обновлено. Что нового:\n" + ("\n".join(news) or "мелкие исправления"))
    return True


def cmd_update(_args):
    if not (ROOT / ".git").exists():
        if update_archive():
            after_update()
        return
    before = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    r = subprocess.run(["git", "-C", str(ROOT), "pull", "--ff-only"], capture_output=True, text=True)
    if r.returncode:
        sys.exit("Обновление не скачалось: " + (r.stderr or r.stdout).strip()[:300])
    after = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if before == after:
        print("Уже последняя версия.")
        return
    log = subprocess.run(["git", "-C", str(ROOT), "diff", before, after, "--", "ЧТО-НОВОГО.md"],
                         capture_output=True, text=True).stdout
    news = [x[1:] for x in log.splitlines() if x.startswith("+") and not x.startswith("+++")]
    print("Обновлено. Что нового:\n" + ("\n".join(news) or "мелкие исправления"))
    after_update()


def after_update():
    if PLATFORM == "mac":
        sync_helpers()
        la = Path.home() / "Library/LaunchAgents"
        if (la / "local.brief.plist").exists() or list(la.glob("local.brief.*.plist")):
            cmd_schedule(["install"])  # расписание было включено: переставляем; выключенное не трогаем


def sync_helpers():
    """Новые или изменённые помощники из обновления: копия в ~/.brief/helpers и подпись на этом Mac.
    Неизменённые не трогаем. Подпись без сертификата повторяемая: тот же файл даёт ту же подпись, разрешения macOS сохраняются."""
    import hashlib
    dst_dir = HOME / "helpers"
    dst_dir.mkdir(parents=True, exist_ok=True)

    def digest(app):
        exe = app / "Contents" / "MacOS"
        return hashlib.sha256(b"".join(x.read_bytes() for x in sorted(exe.glob("*")) if x.is_file())).hexdigest() \
            if exe.exists() else ""
    known_f = HOME / "state" / "helpers.json"  # подпись меняет файл, поэтому сравниваем с исходником прошлой установки
    try:
        known = json.loads(known_f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    for app in sorted((ROOT / "helpers").glob("*.app")):
        dst = dst_dir / app.name
        src = digest(app)
        if dst.exists() and known.get(app.name) == src:
            continue
        known[app.name] = src
        subprocess.run(["rm", "-rf", str(dst)])
        ok = subprocess.run(["ditto", "--noextattr", str(app), str(dst)]).returncode == 0 and \
            subprocess.run(["xattr", "-cr", str(dst)]).returncode == 0 and \
            subprocess.run(["codesign", "--force", "-s", "-", str(dst)], capture_output=True).returncode == 0
        print(f"{'обновлён' if ok else 'НЕ подписался'} помощник {app.stem}")
    known_f.parent.mkdir(parents=True, exist_ok=True)
    known_f.write_text(json.dumps(known, ensure_ascii=False), encoding="utf-8")


def cmd_setup(args):
    sys.path.insert(0, str(CORE))
    import wizard  # noqa: E402
    wizard.main(args)


COMMANDS = {"setup": cmd_setup, "run": cmd_run, "agent": cmd_agent, "agents": cmd_agents, "connectors": cmd_connectors,
            "profile": cmd_profile, "audit": cmd_audit, "doctor": cmd_doctor, "schedule": cmd_schedule, "tick": cmd_tick,
            "update": cmd_update}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(0 if len(sys.argv) < 2 else 1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
