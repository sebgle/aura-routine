"""
aura.scheduler — fires the routines at the right time, every day, by itself.

Runs inside the web app rather than as a separate service, so there is one
process to keep alive instead of two. It wakes every 20 seconds, checks the
clock against settings.json, and starts a routine if one is due.

Two things it guards against:

  * Firing twice. The date of the last fire is written to disk, so restarting
    the app at 06:45 does not start a second sunrise.

  * Firing far too late. If the PC was off all morning and boots at 09:00, a
    full 30-minute sunrise would be absurd. Inside a grace window the ramp is
    shortened so it still lands on the wake time; past that, the routine is
    skipped for the day.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

CHECK_INTERVAL_S = 20

# How late a routine may start and still be worth running. Past this, skip the
# day rather than run a sunrise at lunchtime.
MORNING_GRACE_MIN = 25
NIGHT_GRACE_MIN = 45


class Scheduler:
    def __init__(self, settings_fn, start_fn, is_busy_fn, state_path: Path, log=print):
        self.settings_fn = settings_fn      # () -> Settings
        self.start_fn = start_fn            # async (which, ramp_skip_s) -> None
        self.is_busy_fn = is_busy_fn        # () -> bool
        self.state_path = state_path
        self.log = log
        self.enabled = True
        self.task: asyncio.Task | None = None
        self._fired = self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return {}

    def _save(self) -> None:
        try:
            self.state_path.write_text(json.dumps(self._fired, indent=2), "utf-8")
        except OSError:
            pass  # never let bookkeeping break the alarm

    def _already_fired(self, which: str, day: str) -> bool:
        return self._fired.get(which) == day

    def _mark(self, which: str, day: str) -> None:
        self._fired[which] = day
        self._save()

    # -- timing -------------------------------------------------------------

    def next_runs(self, now: datetime | None = None) -> dict:
        """What the UI shows: when each routine is next due."""
        now = now or datetime.now()
        s = self.settings_fn()
        today = now.replace(second=0, microsecond=0)
        out = {}
        for which, dt in (("morning", s.ramp_start_dt(today)),
                          ("night", s.night_start_dt(today))):
            when = dt if dt > now else dt + timedelta(days=1)
            if self._already_fired(which, dt.strftime("%Y-%m-%d")) and dt <= now:
                when = dt + timedelta(days=1)
            out[which] = {
                "at": when.strftime("%H:%M"),
                "in_min": max(0, round((when - now).total_seconds() / 60)),
                "last_fired": self._fired.get(which),
            }
        return out

    def _due(self, now: datetime) -> tuple[str, float] | None:
        """Returns (which, ramp_skip_seconds) if something should start now."""
        s = self.settings_fn()
        day = now.strftime("%Y-%m-%d")

        ramp_start = s.ramp_start_dt(now)
        late = (now - ramp_start).total_seconds()
        if (not self._already_fired("morning", day)
                and 0 <= late <= MORNING_GRACE_MIN * 60):
            return "morning", late

        night_start = s.night_start_dt(now)
        late_n = (now - night_start).total_seconds()
        if (not self._already_fired("night", day)
                and 0 <= late_n <= NIGHT_GRACE_MIN * 60):
            return "night", 0.0
        return None

    # -- loop ---------------------------------------------------------------

    async def _loop(self) -> None:
        self.log("scheduler running")
        while True:
            try:
                await asyncio.sleep(CHECK_INTERVAL_S)
                if not self.enabled or self.is_busy_fn():
                    continue
                now = datetime.now()
                due = self._due(now)
                if not due:
                    continue
                which, skip = due
                self._mark(which, now.strftime("%Y-%m-%d"))
                if skip > 60:
                    self.log(f"{which} is {skip / 60:.0f} min late — shortening the ramp")
                self.log(f"scheduler: starting {which}")
                await self.start_fn(which, skip)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - the loop must never die
                self.log(f"scheduler error: {e}")

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self.task:
            self.task.cancel()
            self.task = None
