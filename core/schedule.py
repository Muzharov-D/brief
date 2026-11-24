# -*- coding: utf-8 -*-
"""Расписание агентов по профилю: cron из шапки агента или из профиля, местное время.
macOS (launchd):
Одно задание local.brief на всех агентов (одна запись «Огонёк» в Объектах входа): launchd будит `brief tick`
в минуты, когда кому-то из агентов пора, а tick сам решает, кого запустить. Если Mac спал, launchd разбудит
tick один раз после пробуждения, и tick запустит пропущенное за последние часы.
Windows (Планировщик заданий): одно задание «Огонёк» будит tick каждую минуту, пока человек вошёл в систему
(Outlook и Telegram доступны только в его сеансе); после сна или выключения tick догоняет пропущенное."""
import itertools
import os
import plistlib
import subprocess
from datetime import datetime
from xml.sax.saxutils import escape
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


# ---------- Windows ----------

TASK = "Огонёк"


def task_xml(pyw, brief_py, user):
    start = datetime.now().strftime("%Y-%m-%dT%H:%M:00")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Огонёк: будит агентов по расписанию</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start}</StartBoundary><Enabled>true</Enabled>
      <Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
    </TimeTrigger>
    <LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>false</Hidden>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit><Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(str(pyw))}</Command><Arguments>-I "{escape(str(brief_py))}" tick</Arguments></Exec>
  </Actions>
</Task>
"""


def _st(*args):
    # schtasks пишет сообщения в кодировке консоли (OEM), ими пользуемся только для показа ошибки
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, encoding="oem", errors="replace")


def owner_windows():
    """Путь к brief.py в задании «Огонёк», "" если задание чужого вида, None если задания нет.
    Читаем через PowerShell в UTF-8: в путях бывает кириллица, а вывод schtasks в кодировке консоли."""
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
          f"$t=Get-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue; "
          "if ($t) { 'ARGS:' + $t.Actions[0].Arguments } else { 'NONE' }")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = r.stdout.strip()
    if r.returncode or out == "NONE" or not out.startswith("ARGS:"):
        return None if (r.returncode == 0 and out == "NONE") else ""
    import re
    m = re.match(r'ARGS:-I "([^"]+)"', out)
    return m.group(1) if m else ""


def apply_windows(action, jobs, pyw, brief_py, tmpdir):
    out = []
    if action in ("install", "remove"):
        _st("/Delete", "/TN", TASK, "/F")
        out.append({"removed": True})
    on = {n: s for n, s in jobs.items() if s["on"] and s["schedule"]}
    if action == "install" and on:
        user = (os.environ.get("USERDOMAIN", "") + "\\" if os.environ.get("USERDOMAIN") else "") + os.environ.get("USERNAME", "")
        f = Path(tmpdir) / "огонёк-задание.xml"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(task_xml(pyw, brief_py, user), encoding="utf-16")
        r = _st("/Create", "/TN", TASK, "/XML", str(f), "/F")
        f.unlink(missing_ok=True)
        out = [{"agent": n, "title": s["title"], "schedule": s["schedule"], "ok": r.returncode == 0,
                **({"error": (r.stderr or r.stdout).strip()} if r.returncode else {})} for n, s in on.items()]
    if action == "list":
        loaded = _st("/Query", "/TN", TASK).returncode == 0
        for name, s in jobs.items():
            out.append({"agent": name, "title": s["title"], "schedule": s["schedule"], "on": s["on"],
                        "loaded": loaded and bool(s["on"] and s["schedule"])})
    return out
