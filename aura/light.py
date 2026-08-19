"""
aura.light — curve maths and bulb I/O, shared by the instrument and the runner.

Split into two halves on purpose:

  * Pure functions (no I/O) — the ramp curve. Testable anywhere, no hardware.
  * AuraLight — the network side. Retries, verification, honest failure.

Nothing here schedules anything or decides when to run. That is the caller's job.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Defaults. Overridable per-instance via RampSpec.
# ---------------------------------------------------------------------------

# WiZ carries `dimming` as an integer percent. pywizlight clamps to a minimum
# of 1 (PilotBuilder._set_brightness). Whether the bulb firmware honours 1% is
# a per-model question — measured with `aura_bulb_test.py floor`.
MIN_DIMMING_PCT = 1
MAX_DIMMING_PCT = 100

# A 1K change is invisible; sending one every few seconds triples the update
# count for nothing. Both kelvin endpoints should be multiples of this.
KELVIN_STEP = 25


@dataclass(frozen=True)
class RampSpec:
    """Everything that defines the shape of a ramp."""

    duration_s: float = 30 * 60
    kelvin_start: int = 2200
    kelvin_end: int = 5000
    # Perceived brightness is roughly a power law of emitted light, so a ramp
    # linear in commanded brightness leaps up early then crawls. The gamma
    # back-loads it. Higher = stays dark longer. 2.2-2.8 is the usual range.
    brightness_gamma: float = 2.4
    # Gentler curve than brightness, so colour stays warm through the first
    # half rather than going cold while the light is still dim.
    kelvin_gamma: float = 1.6
    update_interval_s: float = 3.0
    # Ramp downward instead of upward (used by the wind-down sequence).
    reverse: bool = False


@dataclass(frozen=True)
class Step:
    at_s: float
    pct: int
    kelvin: int


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def dimming_pct_at(t: float, spec: RampSpec) -> int:
    """Commanded dimming percent (1-100) at normalised ramp position t in [0,1]."""
    t = min(1.0, max(0.0, t))
    raw = t**spec.brightness_gamma
    pct = round(MIN_DIMMING_PCT + raw * (MAX_DIMMING_PCT - MIN_DIMMING_PCT))
    return int(min(MAX_DIMMING_PCT, max(MIN_DIMMING_PCT, pct)))


def kelvin_at(t: float, spec: RampSpec) -> int:
    """Commanded colour temperature at normalised ramp position t, quantised."""
    t = min(1.0, max(0.0, t))
    exact = spec.kelvin_start + (t**spec.kelvin_gamma) * (
        spec.kelvin_end - spec.kelvin_start
    )
    return int(round(exact / KELVIN_STEP) * KELVIN_STEP)


def pct_to_brightness255(pct: int) -> int:
    """
    Convert dimming percent to pywizlight's 0-255 brightness argument.

    pywizlight converts back with round(value / 255 * 100); this inverts it.
    Verified to round-trip exactly for every percent 1..100 (see selftest).
    """
    return int(min(255, max(1, round(pct * 255 / 100))))


def build_ramp(spec: RampSpec) -> list[Step]:
    """
    Build the whole schedule up front.

    Deduplicated: consecutive updates commanding an identical (percent, kelvin)
    pair are dropped, because sending them changes nothing. For a 30-minute
    ramp this is the difference between ~590 and ~183 updates.
    """
    steps: list[Step] = []
    last: tuple[int, int] | None = None
    n = max(1, int(spec.duration_s // spec.update_interval_s))
    for i in range(n + 1):
        elapsed = min(spec.duration_s, i * spec.update_interval_s)
        t = elapsed / spec.duration_s if spec.duration_s else 1.0
        if spec.reverse:
            t = 1.0 - t
        pct, kelvin = dimming_pct_at(t, spec), kelvin_at(t, spec)
        if (pct, kelvin) != last:
            steps.append(Step(elapsed, pct, kelvin))
            last = (pct, kelvin)
    return steps


def build_floor_probe(max_pct: int, hold_s: float, kelvin: int) -> list[Step]:
    """Step 1%..max_pct, holding each level, at a fixed colour temperature."""
    return [
        Step(i * hold_s, pct, kelvin)
        for i, pct in enumerate(range(MIN_DIMMING_PCT, max_pct + 1))
    ]


def steps_from(steps: list[Step], skip_s: float) -> list[Step]:
    """
    Drop the first `skip_s` of a schedule and rebase the timeline to zero.

    Used when a run starts late: rather than skipping the sunrise entirely or
    running a full-length one that finishes after you needed to be awake, jump
    into the middle so it still lands on time.
    """
    if skip_s <= 0:
        return steps
    remaining = [s for s in steps if s.at_s >= skip_s]
    if not remaining:
        return steps[-1:]
    # Re-issue the level that was current at skip_s, so we don't start mid-gap.
    prior = [s for s in steps if s.at_s < skip_s]
    head = (
        [Step(0.0, prior[-1].pct, prior[-1].kelvin)]
        if prior and remaining[0].at_s > skip_s
        else []
    )
    return head + [Step(s.at_s - skip_s, s.pct, s.kelvin) for s in remaining]


# ---------------------------------------------------------------------------
# Network side
# ---------------------------------------------------------------------------


def default_broadcast() -> str:
    """Best-effort local broadcast address, e.g. 192.168.1.255."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packets sent; this only selects the outbound interface.
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        return "255.255.255.255"
    finally:
        s.close()
    return ".".join(ip.split(".")[:3]) + ".255"


class BulbUnreachable(RuntimeError):
    """The bulb did not answer. Almost always: lamp switched off at the wall."""


class EnvironmentBroken(RuntimeError):
    """
    The environment is wrong, not the hardware.

    Worth its own type: the single most likely way this breaks in production is
    Task Scheduler being pointed at the system Python instead of the venv, and
    that must not be reported as "bulb unreachable" at 7am.
    """


@dataclass
class AuraLight:
    """
    A WiZ bulb, with the retry behaviour a 7am alarm actually needs.

    The common real-world failure is not a crash — it is the lamp being
    switched off at the socket, or the network not being up yet after a
    reboot. Both look like silence, so both are retried and then reported
    loudly rather than swallowed.
    """

    ip: str
    log: object = print
    _light: object = field(default=None, repr=False)

    async def connect(self, retries: int = 30, delay_s: float = 10.0) -> None:
        """Reach the bulb, retrying. Raises BulbUnreachable if it never answers."""
        try:
            from pywizlight import wizlight
        except ImportError as e:  # a broken venv should not look like a hardware fault
            raise EnvironmentBroken(
                "pywizlight is not installed in this interpreter. "
                "Activate the venv, or check that the scheduled task points at "
                ".venv\\Scripts\\python.exe and not the system Python."
            ) from e

        self._light = wizlight(self.ip)
        for attempt in range(1, retries + 1):
            try:
                await self._light.updateState()
                self.log(f"bulb reachable at {self.ip} (attempt {attempt})")
                return
            except Exception as e:  # noqa: BLE001 - any failure means "not yet"
                if attempt == retries:
                    raise BulbUnreachable(
                        f"no response from {self.ip} after {retries} attempts "
                        f"over {retries * delay_s / 60:.1f} min: {e}"
                    ) from e
                if attempt == 1 or attempt % 6 == 0:
                    self.log(f"  no response yet (attempt {attempt}/{retries}) ...")
                await asyncio.sleep(delay_s)

    async def apply(self, pct: int, kelvin: int, retries: int = 3) -> bool:
        """Push one level. Returns False if it could not be delivered."""
        from pywizlight import PilotBuilder

        for attempt in range(1, retries + 1):
            try:
                await self._light.turn_on(
                    PilotBuilder(
                        brightness=pct_to_brightness255(pct), colortemp=kelvin
                    )
                )
                return True
            except Exception as e:  # noqa: BLE001
                if attempt == retries:
                    self.log(f"  ! failed to set {pct}% {kelvin}K: {e}")
                    return False
                await asyncio.sleep(1.0)
        return False

    async def off(self) -> None:
        await self._light.turn_off()

    async def play(self, steps: list[Step], label: str = "ramp") -> dict:
        """
        Walk a schedule in real time.

        Returns a summary rather than raising, because a partial sunrise is
        still better than none and the caller needs to log what happened.
        """
        total = steps[-1].at_s if steps else 0.0
        self.log(f"{label}: {len(steps)} updates over {total / 60:.1f} min")
        sent = failed = 0
        t0 = time.monotonic()
        for s in steps:
            drift = s.at_s - (time.monotonic() - t0)
            if drift > 0:
                await asyncio.sleep(drift)
            if await self.apply(s.pct, s.kelvin):
                sent += 1
            else:
                failed += 1
            self.log(f"  t={s.at_s:7.1f}s  dimming={s.pct:3d}%  {s.kelvin}K")
        result = {"sent": sent, "failed": failed, "total": len(steps)}
        self.log(f"{label}: done — {sent} sent, {failed} failed")
        return result
