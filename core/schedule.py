# -*- coding: utf-8 -*-
"""Расписание агентов в macOS (launchd) по профилю: cron из шапки агента или из профиля, местное время.
Одно задание local.brief на всех агентов (одна запись «Огонёк» в Объектах входа): launchd будит `brief tick`
в минуты, когда кому-то из агентов пора, а tick сам решает, кого запустить. Если Mac спал, launchd разбудит
tick один раз после пробуждения, и tick запустит пропущенное за последние часы."""
import itertools
import os
import plistlib
import subprocess
from pathlib import Path

LA = Path.home() / "Library/LaunchAgents"
PREFIX = "local.brief."  # старые задания по агенту, их убираем
LABEL = "local.brief"
APP_ID = "local.brief.app"


def field(spec, lo, hi):
    vals = set()
    for part in spec.split(","):
        rng, _, step = part.partition("/")
        step = int(step or 1)
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = map(int, rng.split("-"))
        else:
            a = b = int(rng)
        vals.update(range(a, b + 1, step))
    return sorted(vals)


def intervals(cron):
    minute, hour, dom, mon, dow = cron.split()
    keys = []
    for name, spec, lo, hi in (("Minute", minute, 0, 59), ("Hour", hour, 0, 23), ("Day", dom, 1, 31),
                               ("Month", mon, 1, 12), ("Weekday", dow, 0, 6)):
        if spec != "*" or name in ("Minute", "Hour"):
            keys.append([(name, v) for v in field(spec, lo, hi)])
    return [dict(c) for c in itertools.product(*keys)]


def due(cron, t):
    """Пора ли агенту с этим cron в минуту t (datetime, местное время)."""
    minute, hour, dom, mon, dow = cron.split()
    wd = (t.weekday() + 1) % 7  # cron: 0 воскресенье
    days = {d % 7 for d in field(dow, 0, 7)}
    return (t.minute in field(minute, 0, 59) and t.hour in field(hour, 0, 23) and t.day in field(dom, 1, 31)
            and t.month in field(mon, 1, 12) and wd in days)


def _lc(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def apply(action, jobs, brief_bin, logdir, app=None):
    """app: исполняемый файл Огонёк.app, если есть; тогда задание и запросы разрешений идут от его имени."""
    uid = os.getuid()
    out = []
    plist = LA / f"{LABEL}.plist"
    if action in ("install", "remove"):
        for old in sorted(LA.glob(PREFIX + "*.plist")):
            _lc("bootout", f"gui/{uid}/{old.stem}")
            old.unlink()
        _lc("bootout", f"gui/{uid}/{LABEL}")
        plist.unlink(missing_ok=True)
        out.append({"removed": True})
    on = {n: s for n, s in jobs.items() if s["on"] and s["schedule"]}
    if action == "install" and on:
        LA.mkdir(parents=True, exist_ok=True)
        Path(logdir).mkdir(parents=True, exist_ok=True)
        moments = {tuple(sorted(i.items())) for s in on.values() for i in intervals(s["schedule"])}
        exe = [str(app)] if app and Path(app).exists() else [str(brief_bin)]
        data = {"Label": LABEL, "ProgramArguments": exe + ["tick"],
                "StartCalendarInterval": [dict(m) for m in sorted(moments)],
                "AbandonProcessGroup": True,  # агенты запускаются отдельно и работают дольше tick
                "StandardOutPath": str(Path(logdir) / "launchd.log"),
                "StandardErrorPath": str(Path(logdir) / "launchd.log"), "ProcessType": "Background"}
        if exe[0] != str(brief_bin):
            data["AssociatedBundleIdentifiers"] = [APP_ID]
        plist.write_bytes(plistlib.dumps(data))
        r = _lc("bootstrap", f"gui/{uid}", str(plist))
        out = [{"agent": n, "title": s["title"], "schedule": s["schedule"], "ok": r.returncode == 0,
                **({"error": r.stderr.strip()} if r.returncode else {})} for n, s in on.items()]
    if action == "list":
        loaded = _lc("print", f"gui/{uid}/{LABEL}").returncode == 0
        for name, s in jobs.items():
            out.append({"agent": name, "title": s["title"], "schedule": s["schedule"], "on": s["on"],
                        "loaded": loaded and bool(s["on"] and s["schedule"])})
    return out
