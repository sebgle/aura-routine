"""
aura.flow — the two routines, written to match the spec directly.

MORNING
  1. ramp dark -> 100%, finishing exactly at wake time
  2. "good morning" at 100%
  3. "get up" a few seconds later
  4. motivational music swells in under "good morning"
  5. nag every 10s until you click I'M UP (never gives up)
  6. tasks announced over the music, ducked, no nagging
  7. click after each task; music keeps running throughout
  8. "all tasks complete", final click, music FADES out. Idle until night.

NIGHT
  1. dim starts at bedtime - (task durations) - buffer
  2. piano starts with the dim
  3. tasks duck the music briefly, click to confirm, no nagging
  4. light drops proportionally per completed task
  5. last task -> "goodnight", light to 1%
  6. light holds at 1% until bedtime, then music fades and the light goes out

Only step 4 of the morning nags. Everything else waits patiently, because you
asked for a checklist, not a drill sergeant.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Phase(str, Enum):
    IDLE = "idle"
    RAMP = "sunrise"
    WAKE = "wake"
    GET_UP = "get_up"
    TASKS = "tasks"
    DONE_PROMPT = "done_prompt"
    DIMMING = "winding down"
    HOLD = "hold"
    FINISHED = "finished"
    STOPPED = "stopped"


@dataclass
class FlowState:
    phase: Phase = Phase.IDLE
    which: str = ""
    task_index: int = -1
    task_text: str | None = None
    tasks_total: int = 0
    waiting_for_click: bool = False
    button_label: str = ""
    nags: int = 0
    light_pct: int | None = None
    music: str | None = None
    started_at: float = 0.0
    elapsed_s: float = 0.0
    speed: float = 1.0
    log: list[str] = field(default_factory=list)


class Flow:
    """Base: shared plumbing for both routines."""

    def __init__(self, settings, speech, player, music, light, log=print,
                 speed: float = 1.0, sim: bool = False,
                 motivation_dir: Path | None = None, piano_dir: Path | None = None,
                 nag_dir: Path | None = None, ramp_skip_s: float = 0.0,
                 hold_s: float | None = None):
        self.s = settings
        self.speech = speech
        self.player = player
        self.music = music
        self.light = light
        self._log = log
        self.speed = max(0.01, speed)
        self.sim = sim
        self.motivation_dir = motivation_dir
        self.piano_dir = piano_dir
        self.nag_dir = nag_dir
        self.ramp_skip_s = max(0.0, ramp_skip_s)
        # Demo runs override the wait-until-bedtime hold; None = real clock.
        self.hold_s = hold_s
        self._nag_i = 0
        self.state = FlowState(speed=self.speed)
        self._click = asyncio.Event()
        self._stop = asyncio.Event()
        self._t0 = time.monotonic()

    # -- plumbing -----------------------------------------------------------

    def _now(self) -> float:
        return (time.monotonic() - self._t0) * self.speed

    def note(self, msg: str) -> None:
        line = f"  t={self._now():7.1f}s  {msg}"
        self.state.log.append(line)
        del self.state.log[:-120]
        self._log(line)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds / self.speed)

    def click(self) -> None:
        self._click.set()

    def stop(self) -> None:
        self._stop.set()
        self._click.set()

    async def wait_click(self, label: str) -> bool:
        """Block until the button is pressed. Returns False if stopped."""
        self._click.clear()
        self.state.waiting_for_click = True
        self.state.button_label = label
        try:
            await self._click.wait()
        finally:
            self.state.waiting_for_click = False
            self.state.button_label = ""
        return not self._stop.is_set()

    async def say(self, line_id: str, duck: bool = True) -> None:
        """Speak over the music, ducking it rather than stopping it."""
        p = self.speech.path_for(line_id) if self.speech else None
        self.note(f"say {line_id}")
        if self.sim or not p or not self.player:
            await self.sleep(2.0)
            return
        if duck and self.music:
            await self.music.duck()
        try:
            await asyncio.to_thread(self.player.say, p, self.s.voice_volume, True)
        finally:
            if duck and self.music:
                await self.music.unduck()

    def nag_clips(self) -> list[Path]:
        from aura.audio import playlist

        return playlist(self.nag_dir) if self.nag_dir else []

    async def play_nag(self) -> None:
        """
        Rotate through your recorded get-up lines.

        Rotation rather than random: random repeats, and a reminder you just
        heard ten seconds ago is one you have already tuned out. Falls back to
        the synthesised line if you have not recorded any.
        """
        clips = self.nag_clips()
        if not clips:
            await self.say("nag_get_up")
            return
        clip = clips[self._nag_i % len(clips)]
        self._nag_i += 1
        self.note(f"nag {clip.name}")
        if self.sim or not self.player:
            await self.sleep(2.0)
            return
        if self.music:
            await self.music.duck()
        try:
            await asyncio.to_thread(self.player.say, clip, self.s.voice_volume, True)
        finally:
            if self.music:
                await self.music.unduck()

    async def set_light(self, pct: int, kelvin: int | None = None) -> None:
        k = kelvin if kelvin is not None else self.s.night_kelvin
        self.state.light_pct = pct
        self.note(f"light {pct}% {k}K")
        if self.sim or not self.light:
            return
        try:
            await self.light.apply(pct, k)
        except Exception as e:  # noqa: BLE001 - a light fault must not kill the routine
            self.note(f"light failed: {e}")

    def start_music(self, folder: Path | None, fade_in_s: float = 0.0) -> None:
        if self.sim or not self.music or not folder:
            self.note(f"music start ({folder.name if folder else 'none'})"
                      + (f", fading in over {fade_in_s:g}s" if fade_in_s else ""))
            self.state.music = "(simulated)" if self.sim else None
            return
        if self.music.start(folder, fade_in_s=fade_in_s):
            self.state.music = self.music.now_playing
            self.note(f"music playing: {self.state.music}")
        else:
            self.note(f"no audio files in {folder.name} — continuing silently")

    async def fade_music(self, seconds: float | None = None) -> None:
        secs = self.s.music_fade_s if seconds is None else seconds
        if self.sim or not self.music:
            self.note(f"music fade out ({secs:g}s)")
            self.state.music = None
            return
        await self.music.fade_out(secs)
        self.state.music = None

    def snapshot(self) -> dict:
        st = self.state
        st.elapsed_s = round(self._now(), 1)
        if self.music and not self.sim and self.music.playing:
            st.music = self.music.now_playing
        return {
            "phase": st.phase.value,
            "which": st.which,
            "task_index": st.task_index,
            "task_text": st.task_text,
            "tasks_total": st.tasks_total,
            "waiting": st.waiting_for_click,
            "button": st.button_label,
            "nags": st.nags,
            "light_pct": st.light_pct,
            "music": st.music,
            "elapsed_s": st.elapsed_s,
            "speed": st.speed,
            "log": st.log[-24:],
        }


class MorningFlow(Flow):
    async def run(self) -> str:
        s, st = self.s, self.state
        st.which = "morning"
        st.tasks_total = len(s.morning_tasks)
        self._t0 = time.monotonic()

        # 1. ramp, finishing at 100% exactly at wake time
        st.phase = Phase.RAMP
        self.note(f"sunrise: dark -> 100% over {s.ramp_minutes:g} min")
        await self.set_light(1, 2200)
        await self._ramp_up()
        if self._stop.is_set():
            st.phase = Phase.STOPPED
            return "stopped"

        # The music starts here, not on the click. It swells from silence under
        # "good morning" — at the start of the fade it is quiet enough that the
        # line sits clearly on top, and by "get up" it is at full and ducks
        # like everything else.
        self.start_music(self.motivation_dir, fade_in_s=s.music_fade_in_s)

        # 2-4. Good morning, then get up, then reminders on a loop — but the
        # button is live from the very first word. If you are already awake
        # when the light reaches full, you should be able to say so straight
        # away rather than sitting through a reminder you do not need.
        st.phase = Phase.WAKE
        self._click.clear()
        st.waiting_for_click = True
        st.button_label = "I'M UP"

        def acked() -> bool:
            return self._click.is_set() or self._stop.is_set()

        async def wake_sequence() -> None:
            # Checked before every line, not only between them: cancellation
            # has latency, and nothing should start speaking in the gap between
            # your click and the task actually stopping.
            if acked():
                return
            # No duck here: the music is still swelling from silence, so the
            # line is clear anyway, and ducking would undo the fade-in.
            await self.say("good_morning", duck=False)
            if acked():
                return
            await self.sleep(s.get_up_delay_s)
            if acked():
                return
            st.phase = Phase.GET_UP
            await self.say("get_up")
            while not acked():
                await self.sleep(s.get_up_nag_interval_s)
                if acked():
                    return
                st.nags += 1
                await self.play_nag()

        seq = asyncio.create_task(wake_sequence())
        try:
            await self._click.wait()
        finally:
            seq.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await seq
            st.waiting_for_click = False
            st.button_label = ""
            # Cut any half-finished line. Once you have said you are up, being
            # told to get up is just noise.
            if not self.sim and self.player:
                with contextlib.suppress(Exception):
                    self.player.stop_all()
        if self._stop.is_set():
            st.phase = Phase.STOPPED
            return "stopped"

        # 6/7. tasks over the music, ducked, no nagging
        st.phase = Phase.TASKS
        for i, task in enumerate(s.morning_tasks):
            st.task_index, st.task_text = i, task.text
            await self.say(f"m{i}")
            if not await self.wait_click("DONE"):
                st.phase = Phase.STOPPED
                return "stopped"
            self.note(f"task done: {task.text}")
        st.task_index, st.task_text = -1, None

        # 8. wrap up, final click, fade
        st.phase = Phase.DONE_PROMPT
        await self.say("morning_done")
        if not await self.wait_click("FINISH"):
            st.phase = Phase.STOPPED
            return "stopped"
        await self.fade_music(s.morning_fade_s)
        st.phase = Phase.FINISHED
        self.note("morning complete")
        return "complete"

    async def _ramp_up(self) -> None:
        """
        Ramp to full, finishing at the wake time.

        If the scheduler fired late — PC asleep, say — skip into the middle of
        the curve rather than running a full-length ramp that would finish
        after you needed to be awake.
        """
        from aura.light import RampSpec, build_ramp, steps_from

        spec = RampSpec(duration_s=self.s.ramp_minutes * 60, kelvin_start=2200,
                        kelvin_end=5000, update_interval_s=3.0)
        steps = build_ramp(spec)
        if self.ramp_skip_s > 0:
            steps = steps_from(steps, self.ramp_skip_s)
            self.note(f"starting {self.ramp_skip_s / 60:.0f} min into the ramp")
        base = time.monotonic()
        for step in steps:
            if self._stop.is_set():
                return
            drift = (step.at_s / self.speed) - (time.monotonic() - base)
            if drift > 0:
                await asyncio.sleep(drift)
            self.state.light_pct = step.pct
            if not self.sim and self.light:
                try:
                    await self.light.apply(step.pct, step.kelvin)
                except Exception as e:  # noqa: BLE001
                    self.note(f"light failed: {e}")


class NightFlow(Flow):
    async def run(self) -> str:
        s, st = self.s, self.state
        st.which = "night"
        st.tasks_total = len(s.evening_tasks)
        self._t0 = time.monotonic()

        # 1/2. dim begins, piano begins with it
        st.phase = Phase.DIMMING
        self.note(f"wind-down: {s.night_start_pct}% -> {s.night_end_pct}% "
                  f"across {st.tasks_total} tasks")
        await self.set_light(s.night_start_pct)
        self.start_music(self.piano_dir)

        # 3/4. each task ducks the music, waits for a click, then the light
        #      drops proportionally. No nagging.
        for i, task in enumerate(s.evening_tasks):
            st.task_index, st.task_text = i, task.text
            await self.say(f"e{i}")
            if not await self.wait_click("DONE"):
                st.phase = Phase.STOPPED
                return "stopped"
            self.note(f"task done: {task.text}")
            await self.set_light(self._pct_after(i + 1))
        st.task_index, st.task_text = -1, None

        # 5. goodnight, enough light to find the bed
        await self.say("goodnight")
        await self.set_light(s.night_end_pct)

        # 6. hold until bedtime, then fade and go dark
        st.phase = Phase.HOLD
        hold_s = self._seconds_until_bedtime()
        self.note(f"holding at {s.night_end_pct}% for {hold_s / 60:.1f} min until bedtime")
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=hold_s / self.speed)
        except asyncio.TimeoutError:
            pass
        if self._stop.is_set():
            st.phase = Phase.STOPPED
            return "stopped"

        await self.fade_music()
        await self.set_light(1)
        if not self.sim and self.light:
            try:
                await self.light.off()
            except Exception as e:  # noqa: BLE001
                self.note(f"light off failed: {e}")
        self.state.light_pct = 0
        st.phase = Phase.FINISHED
        self.note("goodnight")
        return "complete"

    def _pct_after(self, completed: int) -> int:
        """Brightness after N completed tasks: a straight walk down to night_end_pct."""
        s = self.s
        n = max(1, len(s.evening_tasks))
        frac = min(1.0, completed / n)
        return max(s.night_end_pct,
                   int(round(s.night_start_pct + frac * (s.night_end_pct - s.night_start_pct))))

    def _seconds_until_bedtime(self) -> float:
        """
        Real seconds left until bedtime.

        Overridable, because a demo run at 3pm would otherwise sit at 1%
        brightness for eight hours waiting for 23:00.
        """
        from datetime import datetime

        if self.hold_s is not None:
            return self.hold_s
        if self.speed > 1.5:
            return self.s.night_buffer_minutes * 60
        now = datetime.now()
        bed = self.s.bed_dt(now)
        if bed < now:
            return 0.0
        return (bed - now).total_seconds()
