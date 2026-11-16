"""Past days, from the dashboard: list recorded/rebuilt days, rebuild a new one, and replay it for practice.

The practice replay runs as a second copy of the app on the next port (8766 by default), so the live
screen keeps running. Nothing here places orders; paper trades made in a replay are kept separately.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import re
import socket
import sys
from datetime import date
from pathlib import Path

from .config import DATA_DIR
from .timeutil import now_ist

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


class Practice:
    def __init__(self, port: int):
        self.port = port
        self.state = "idle"        # idle | rebuilding | starting | ready | failed
        self.day: str | None = None
        self.message = ""
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._log = DATA_DIR / "logs" / "practice.log"
        atexit.register(self._kill)

    # ------------------------------------------------------------ days
    @staticmethod
    def days() -> list[dict]:
        folder = DATA_DIR / "sessions"
        today = now_ist().date().isoformat()
        out = []
        if folder.exists():
            for p in folder.iterdir():
                if not p.is_dir() or not any(p.iterdir()):
                    continue
                day = p.name[:10]
                if not DAY_RE.match(day):
                    continue
                rebuilt = p.name.endswith("-rebuilt")
                if rebuilt and not (p / "meta.json").exists():
                    continue
                out.append({"day": day, "folder": p.name, "kind": "rebuilt" if rebuilt else "recorded",
                            "today": day == today and not rebuilt})
        out.sort(key=lambda d: (d["day"], d["kind"] == "recorded"), reverse=True)
        return out

    def status(self) -> dict:
        if self.state == "ready" and (self._proc is None or self._proc.returncode is not None):
            self.state, self.message = "idle", "The practice replay stopped."
        tail = ""
        try:
            lines = self._log.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(l for l in lines[-12:] if l.strip())
        except OSError:
            pass
        return {"available": True, "state": self.state, "day": self.day, "message": self.message,
                "url": f"http://127.0.0.1:{self.port}/", "days": self.days(), "log": tail}

    # ---------------------------------------------------------- control
    async def start(self, day: str, folder: str | None, stocks: bool) -> dict:
        if not DAY_RE.match(day or ""):
            return {"ok": False, "error": "Pick a date (YYYY-MM-DD)."}
        try:
            d = date.fromisoformat(day)
        except ValueError:
            return {"ok": False, "error": "That is not a valid date."}
        if d > now_ist().date():
            return {"ok": False, "error": "That day has not happened yet."}
        if folder and (not re.fullmatch(r"\d{4}-\d{2}-\d{2}(-rebuilt)?", folder)
                       or not (DATA_DIR / "sessions" / folder).is_dir()):
            return {"ok": False, "error": "Unknown saved day."}
        await self.stop()
        self.day, self.message = day, ""
        self._task = asyncio.get_running_loop().create_task(self._run(day, folder, stocks))
        return {"ok": True}

    async def _exec(self, *args: str) -> asyncio.subprocess.Process:
        self._log.parent.mkdir(parents=True, exist_ok=True)
        f = open(self._log, "ab")
        try:
            return await asyncio.create_subprocess_exec(sys.executable, "-m", "app", *args, cwd=str(ROOT),
                                                        stdout=f, stderr=asyncio.subprocess.STDOUT,
                                                        stdin=asyncio.subprocess.DEVNULL)
        finally:
            f.close()

    async def _run(self, day: str, folder: str | None, stocks: bool) -> None:
        try:
            self._log.parent.mkdir(parents=True, exist_ok=True)
            self._log.write_text("", encoding="utf-8")
            if folder is None:
                rec = DATA_DIR / "sessions" / f"{day}-rebuilt"
                if not (rec / "meta.json").exists() or stocks:
                    self.state = "rebuilding"
                    self.message = f"Building {day}" + (" (stocks download from your broker: a few minutes)" if stocks
                                                         else " (from your saved files, or a quick download)") + "…"
                    self._proc = await self._exec("rebuild", day, *([] if stocks else ["--indices-only"]))
                    code = await self._proc.wait()
                    self._proc = None
                    if code != 0 or not (rec / "meta.json").exists():
                        self.state = "failed"
                        self.message = f"Could not rebuild {day} (market holiday or weekend? see the log below)."
                        return
                folder = rec.name
            self.state, self.message = "starting", f"Starting the replay of {day}…"
            self._proc = await self._exec("--replay", str(DATA_DIR / "sessions" / folder), "--port", str(self.port),
                                          "--no-browser", "--speed", "30")
            for _ in range(80):
                await asyncio.sleep(0.5)
                if self._proc.returncode is not None:
                    self.state, self.message = "failed", "The replay stopped while starting (see the log below)."
                    return
                if _port_open(self.port):
                    self.state, self.message = "ready", f"{day} is ready to practise."
                    return
            self.state, self.message = "failed", "The replay did not start in time (see the log below)."
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - depends on the machine
            log.warning("Practice day failed: %s", e)
            self.state, self.message = "failed", f"{type(e).__name__}: {e}"

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        p = self._proc
        self._proc = None
        if p is not None and p.returncode is None:
            p.terminate()
            try:
                await asyncio.wait_for(p.wait(), 5)
            except asyncio.TimeoutError:
                p.kill()
        self.state = "idle"
        self.message = ""

    def _kill(self) -> None:
        p = self._proc
        if p is not None and p.returncode is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass
