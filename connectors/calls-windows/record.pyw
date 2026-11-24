"""Запись созвона на этом компьютере: микрофон (это «Я») и звук собеседников из колонок/наушников
(«Собеседник») пишутся раздельно, потом расшифровываются локально. Ничего не уходит в интернет.

Результат в папке Записи рабочей папки Огонька: «ГГГГ-ММ-ДД ЧЧ-ММ <тема>.txt» с расшифровкой и две
дорожки .wav (.mic.wav и .others.wav, 16 кГц, около 115 МБ на час каждая).
Протокол по расшифровке делает агент «После созвона» (коннектор calls-windows).
"""
import re
import sys
import threading
import time
import tkinter as tk
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent  # папка Огонька
STT = ROOT / "connectors" / "voice-whisper"


def records_dir():
    """Записи в рабочей папке из профиля Огонька; без профиля в ~/Brief/Записи."""
    try:
        sys.path.insert(0, str(ROOT / "core"))
        import brief
        return brief.workspace(brief.load_profile()) / "Записи"
    except Exception:  # noqa: BLE001
        return Path.home() / "Brief" / "Записи"


OUT = records_dir()
RATE = 48000
BLOCK = RATE // 10  # 0,1 с
TARGET = 16000


class Track:
    """Блоки с отметкой времени: тишина в звуке собеседников (WASAPI не отдаёт данных, пока
    ничего не играет) восстанавливается по часам, чтобы реплики не съезжали."""

    def __init__(self):
        self.blocks = []

    def add(self, data):
        mono = data.mean(axis=1) if data.ndim > 1 else data
        n = len(mono) // 3 * 3
        small = mono[:n].reshape(-1, 3).mean(axis=1)  # 48 кГц в 16 кГц с усреднением
        self.blocks.append((time.monotonic(), (np.clip(small, -1, 1) * 32767).astype(np.int16)))

    def render(self, t0, total):
        """total в отсчётах 16 кГц; int16, чтобы час записи занимал ~115 МБ памяти."""
        out = np.zeros(total, dtype=np.int16)
        for t, b in self.blocks:
            end = min(total, int((t - t0) * TARGET))
            start = max(0, end - len(b))
            if end <= start:
                continue
            out[start:end] = b[len(b) - (end - start):]
        return out


def capture(device, track, stop, errors, label):
    try:
        with device.recorder(samplerate=RATE, channels=None, blocksize=BLOCK) as rec:
            while not stop.is_set():
                track.add(rec.record(numframes=BLOCK))
    except Exception as e:  # noqa: BLE001
        errors.append(f"{label}: {e}")


def save_wav(path, x):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TARGET)
        w.writeframes(x.astype("<i2").tobytes())


def mmss(sec):
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


class App:
    def __init__(self, root):
        self.root = root
        root.title("Огонёк: запись созвона")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        tk.Label(root, text="Тема (можно оставить пустой):").pack(padx=12, pady=(12, 2), anchor="w")
        self.topic = tk.Entry(root, width=42)
        self.topic.pack(padx=12)
        self.btn = tk.Button(root, text="Начать запись", width=24, command=self.toggle)
        self.btn.pack(padx=12, pady=10)
        self.status = tk.Label(root, text="Пишется микрофон и звук собеседников из колонок или наушников.",
                               wraplength=320, justify="left")
        self.status.pack(padx=12, pady=(0, 12))
        self.recording = False
        root.protocol("WM_DELETE_WINDOW", self.close)

    def toggle(self):
        if self.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        import soundcard as sc
        self.stop = threading.Event()
        self.mic, self.loop, self.errors = Track(), Track(), []
        self.t0, self.started = time.monotonic(), datetime.now()
        speaker = sc.default_speaker()
        devices = [(sc.default_microphone(), self.mic, "микрофон"),
                   (sc.get_microphone(id=str(speaker.name), include_loopback=True), self.loop, "звук собеседников")]
        self.threads = [threading.Thread(target=capture, args=(d, t, self.stop, self.errors, lbl), daemon=True)
                        for d, t, lbl in devices]
        for th in self.threads:
            th.start()
        self.recording = True
        self.btn.config(text="Остановить и расшифровать")
        self.tick()

    def tick(self):
        if not self.recording:
            return
        sec = time.monotonic() - self.t0
        err = f"\nОшибка: {'; '.join(self.errors)}" if self.errors else ""
        self.status.config(text=f"Идёт запись {mmss(sec)}{err}")
        self.root.after(500, self.tick)

    def stop_recording(self):
        self.stop.set()
        for th in self.threads:
            th.join(timeout=3)
        self.recording = False
        ended = datetime.now()
        total = int((time.monotonic() - self.t0) * TARGET)
        mic, loop = self.mic.render(self.t0, total), self.loop.render(self.t0, total)
        self.mic = self.loop = None
        topic = re.sub(r'[\\/:*?"<>|]', " ", self.topic.get()).strip() or "Созвон"
        OUT.mkdir(parents=True, exist_ok=True)
        base = OUT / f"{self.started:%Y-%m-%d %H-%M} {topic}"
        tmp_mic, tmp_loop = base.with_name(base.name + ".mic.wav"), base.with_name(base.name + ".others.wav")
        save_wav(tmp_mic, mic)
        save_wav(tmp_loop, loop)
        del mic, loop
        self.btn.config(state="disabled", text="Расшифровка…")
        self.status.config(text="Расшифровываю на этом компьютере. Окно можно свернуть; закроется само.")
        threading.Thread(target=self.transcribe, args=(base, tmp_mic, tmp_loop, topic, ended), daemon=True).start()

    def transcribe(self, base, tmp_mic, tmp_loop, topic, ended):
        try:
            sys.path.insert(0, str(STT))
            import stt
            lines = [(s, "Я", t) for s, _, t in stt.segments(tmp_mic)]
            lines += [(s, "Собеседник", t) for s, _, t in stt.segments(tmp_loop)]
            lines.sort()
            minutes = round((ended - self.started).total_seconds() / 60)
            head = [f"Тема: {topic}", f"Начало: {self.started:%Y-%m-%d %H:%M}", f"Конец: {ended:%Y-%m-%d %H:%M}",
                    f"Минут: {minutes}", f"Id: rec-{self.started:%Y%m%d-%H%M%S}",
                    "Дорожки: «Я» = микрофон этого компьютера, «Собеседник» = все остальные участники вместе", ""]
            body = [f"[{mmss(s)}] {who}: {t}" for s, who, t in lines]
            base.with_name(base.name + ".txt").write_text("\n".join(head + body) + "\n", encoding="utf-8")
            self.root.after(0, lambda: self.done(f"Готово: {base.name}.txt"))
        except Exception as e:  # noqa: BLE001
            err = str(e)
            self.root.after(0, lambda: self.done(f"Расшифровка не удалась: {err}. Звук сохранён рядом (.wav)"))

    def done(self, msg):
        self.status.config(text=msg + "\nОкно закроется через 10 секунд.")
        self.btn.config(state="disabled", text="Готово")
        self.root.after(10000, self.root.destroy)

    def close(self):
        if self.recording:
            self.stop_recording()  # закрытие окна во время записи = остановить и расшифровать
            return
        if str(self.btn.cget("state")) == "disabled" and self.btn.cget("text") == "Расшифровка…":
            return  # не даём закрыть, пока идёт расшифровка
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
