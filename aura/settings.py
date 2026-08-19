"""
aura.settings — the whole configuration, in one JSON file the GUI owns.

Only five things are user-facing: wake time, bedtime, morning tasks, evening
tasks, and the audio libraries. Everything else has a defensible default and
stays out of the way.

Evening tasks carry a duration, because that is what lets the system work
backwards from bedtime to decide when to start dimming.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path


@dataclass
class Task:
    text: str
    minutes: float = 3.0     # evening only; how long it actually takes you


@dataclass
class Settings:
    wake_time: str = "07:00"
    bedtime: str = "23:00"

    # Morning: the ramp finishes AT wake_time, so it starts this much earlier.
    ramp_minutes: float = 30.0
    # Seconds between "good morning" and "get up".
    get_up_delay_s: float = 8.0
    # The get-up nag repeats this often, and never gives up (your call).
    get_up_nag_interval_s: float = 10.0

    # Night: tasks finish this long before bedtime, leaving a quiet dim window
    # to actually get into bed.
    night_buffer_minutes: float = 15.0
    # Brightness at the start of the wind-down, and after the last task.
    night_start_pct: int = 60
    night_end_pct: int = 1
    night_kelvin: int = 2200
    # The morning music swells in under "good morning" rather than starting
    # at full. Long enough that the first line sits clearly on top of it.
    music_fade_in_s: float = 14.0

    # Fade lengths differ by intent. At bedtime a long fade helps you drift
    # off, so the music should disappear without you noticing. In the morning
    # you have just finished and are walking away — a long fade is dead air.
    music_fade_s: float = 20.0        # evening, at bedtime
    morning_fade_s: float = 3.0       # after the final click

    # Which synthesised voice speaks the lines you have not recorded yourself.
    # A recording always wins over this - see aura.speech.SpeechLibrary.
    voice_name: str = "en-US-JennyNeural"

    voice_volume: float = 0.85
    music_volume: float = 0.45
    # Music level while a line is spoken. 0.0 = fully silent under the voice,
    # which is what an instruction needs; raise it if you want a bed of music
    # underneath instead.
    duck_volume: float = 0.0

    morning_tasks: list[Task] = field(default_factory=lambda: [
        Task("Drink a glass of water"),
        Task("Brush your teeth"),
        Task("Make your bed"),
    ])
    evening_tasks: list[Task] = field(default_factory=lambda: [
        Task("Put your phone on the desk", 2),
        Task("Take your contacts out", 3),
        Task("Brush your teeth", 4),
    ])

    # -- derived timings ----------------------------------------------------

    @property
    def evening_total_minutes(self) -> float:
        return sum(t.minutes for t in self.evening_tasks)

    def wake_dt(self, today: datetime) -> datetime:
        hh, mm = (int(x) for x in self.wake_time.split(":"))
        return today.replace(hour=hh, minute=mm, second=0, microsecond=0)

    def bed_dt(self, today: datetime) -> datetime:
        hh, mm = (int(x) for x in self.bedtime.split(":"))
        return today.replace(hour=hh, minute=mm, second=0, microsecond=0)

    def ramp_start_dt(self, today: datetime) -> datetime:
        return self.wake_dt(today) - timedelta(minutes=self.ramp_minutes)

    def night_start_dt(self, today: datetime) -> datetime:
        """
        When the dimming begins.

        bedtime - (every task's duration) - buffer. The buffer is why the light
        sits at 1% for a while at the end instead of going out the instant you
        finish brushing your teeth.
        """
        return self.bed_dt(today) - timedelta(
            minutes=self.evening_total_minutes + self.night_buffer_minutes
        )

    def schedule_summary(self, today: datetime | None = None) -> dict:
        today = today or datetime.now()
        return {
            "ramp_start": self.ramp_start_dt(today).strftime("%H:%M"),
            "wake": self.wake_time,
            "night_start": self.night_start_dt(today).strftime("%H:%M"),
            "tasks_end": (
                self.bed_dt(today) - timedelta(minutes=self.night_buffer_minutes)
            ).strftime("%H:%M"),
            "bedtime": self.bedtime,
            "evening_total_minutes": self.evening_total_minutes,
            "buffer_minutes": self.night_buffer_minutes,
        }

    # -- persistence --------------------------------------------------------

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @staticmethod
    def from_dict(d: dict) -> "Settings":
        known = Settings.__dataclass_fields__
        kw = {k: v for k, v in d.items() if k in known and k not in
              ("morning_tasks", "evening_tasks")}
        s = Settings(**kw)
        if "morning_tasks" in d:
            s.morning_tasks = [Task(**t) for t in d["morning_tasks"]]
        if "evening_tasks" in d:
            s.evening_tasks = [Task(**t) for t in d["evening_tasks"]]
        return s


def load(path: Path) -> Settings:
    if not path.exists():
        s = Settings()
        save(path, s)
        return s
    return Settings.from_dict(json.loads(path.read_text("utf-8")))


def save(path: Path, s: Settings) -> None:
    path.write_text(json.dumps(s.to_dict(), indent=2), "utf-8")


# ---------------------------------------------------------------------------
# The spoken lines. Fixed phrases plus one per task.
# ---------------------------------------------------------------------------

FIXED_LINES = {
    "good_morning": "Good morning.",
    "get_up": "Time to get up. Feet on the floor.",
    "nag_get_up": "Get up.",
    "morning_done": "All tasks complete. Go make the most of today.",
    "goodnight": "Goodnight. Sleep well.",
}


def line_specs(s: Settings) -> list[dict]:
    """Every line that needs rendering: the fixed ones plus every task."""
    out = [{"id": k, "text": v, "kind": "fixed"} for k, v in FIXED_LINES.items()]
    for i, t in enumerate(s.morning_tasks):
        out.append({"id": f"m{i}", "text": t.text, "kind": "morning_task"})
    for i, t in enumerate(s.evening_tasks):
        out.append({"id": f"e{i}", "text": t.text, "kind": "evening_task"})
    return out
