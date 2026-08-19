"""
aura.audio — voice on top of music, on the wired soundbar.

Two independent layers:

  MusicPlayer   one continuous shuffled stream (piano at night, motivational
                in the morning). Ducks under the voice and fades out at the
                end rather than cutting off.

  Player        one-shot voice clips. Plays on a mixer channel, so it mixes
                over the music instead of replacing it.

Announcements never stop the music — they duck it. Nothing in this system
stops audio abruptly except the stop button.
"""

from __future__ import annotations

import array
import asyncio
import math
import os
import random
import wave
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

# Inaudible to adults, cheap to synthesise, enough signal to stop a soundbar's
# auto-standby from sleeping and eating the first word of an announcement.
KEEPALIVE_HZ = 19_000
KEEPALIVE_AMPLITUDE = 0.002  # ~ -54 dBFS
SAMPLE_RATE = 44_100

AUDIO_EXTS = {".mp3", ".ogg", ".wav", ".flac", ".m4a"}


def make_keepalive_wav(path: Path, seconds: float = 5.0) -> Path:
    """Synthesise a looping inaudible tone. Stdlib only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    n = int(SAMPLE_RATE * seconds)
    peak = int(32767 * KEEPALIVE_AMPLITUDE)
    samples = array.array(
        "h",
        (int(peak * math.sin(2 * math.pi * KEEPALIVE_HZ * i / SAMPLE_RATE)) for i in range(n)),
    )
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(samples.tobytes())
    return path


class AudioUnavailable(RuntimeError):
    """No usable audio device. Distinct from 'played but you couldn't hear it'."""


def _pygame():
    try:
        import pygame
    except ImportError as e:
        raise AudioUnavailable(
            "no pygame module — run `pip install pygame-ce`. "
            "Note pygame-ce, not pygame: upstream pygame has no Windows wheels "
            "past cp313 and will try (and fail) to build from source."
        ) from e
    return pygame


def playlist(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in AUDIO_EXTS)


@dataclass
class Player:
    """One-shot voice clips, mixed over whatever else is playing."""

    volume: float = 0.8
    _ready: bool = field(default=False, repr=False)
    _keepalive: object = field(default=None, repr=False)

    def start(self, log=None) -> None:
        """
        Open the audio device.

        Deliberately cautious. SDL can take the whole process down at the C
        level if it dislikes the device parameters — no Python traceback, just
        an exit — so this tries plain defaults first and only then falls back
        to explicitly-named drivers. Each attempt is logged before it runs, so
        if the process does die the last line printed names the culprit.
        """
        if self._ready:
            return
        log = log or (lambda m: None)
        pygame = _pygame()

        attempts = [
            ("defaults", None, {}),
            ("directsound", "directsound", {}),
            ("wasapi", "wasapi", {}),
            ("explicit 44.1k stereo", None,
             dict(frequency=SAMPLE_RATE, size=-16, channels=2, buffer=1024)),
        ]
        errors = []
        for name, driver, kwargs in attempts:
            if driver:
                os.environ["SDL_AUDIODRIVER"] = driver
            else:
                os.environ.pop("SDL_AUDIODRIVER", None)
            log(f"audio: trying {name} ...")
            try:
                with suppress(Exception):
                    pygame.mixer.quit()
                pygame.mixer.init(**kwargs)
                pygame.mixer.set_num_channels(8)
                got = pygame.mixer.get_init()
                log(f"audio: opened via {name} -> {got}")
                self._ready = True
                return
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {e}")
                log(f"audio: {name} failed - {e}")

        os.environ.pop("SDL_AUDIODRIVER", None)
        raise AudioUnavailable("could not open an audio device. Tried: "
                               + "; ".join(errors))

    def _require(self):
        if not self._ready:
            self.start()
        return _pygame()

    def play(self, path: Path, volume: float | None = None, block: bool = False):
        pygame = self._require()
        if not Path(path).exists():
            raise FileNotFoundError(path)
        sound = pygame.mixer.Sound(str(path))
        ch = pygame.mixer.find_channel(force=True)
        ch.set_volume(self.volume if volume is None else max(0.0, min(1.0, volume)))
        ch.play(sound)
        if block:
            while ch.get_busy():
                pygame.time.wait(40)
        return ch

    def say(self, path: Path, volume: float | None = None, block: bool = True):
        return self.play(path, volume=volume, block=block)

    def keepalive_start(self, path: Path) -> None:
        if self._keepalive is not None:
            return
        pygame = self._require()
        make_keepalive_wav(path)
        ch = pygame.mixer.Channel(7)  # reserved so voice never evicts it
        ch.set_volume(1.0)            # the tone's amplitude is what makes it inaudible
        ch.play(pygame.mixer.Sound(str(path)), loops=-1)
        self._keepalive = ch

    def keepalive_stop(self) -> None:
        if self._keepalive is not None:
            self._keepalive.stop()
            self._keepalive = None

    @property
    def keepalive_running(self) -> bool:
        return self._keepalive is not None

    def stop_all(self) -> None:
        pygame = self._require()
        for i in range(pygame.mixer.get_num_channels()):
            ch = pygame.mixer.Channel(i)
            if ch is not self._keepalive:
                ch.stop()


class MusicPlayer:
    """
    A continuous shuffled background stream.

    Uses pygame.mixer.music (a single streamed channel) rather than a Sound on
    a mixer channel: it streams from disk instead of loading whole files, and
    it has native fadeout. There is only ever one background stream, so the
    single-stream limitation costs nothing.
    """

    def __init__(self, player: Player, volume: float = 0.5, duck_volume: float = 0.2,
                 log=print):
        self.player = player
        self.volume = volume
        self.duck_volume = duck_volume
        self.log = log
        self.tracks: list[Path] = []
        self.index = 0
        self.playing = False
        self._ducked = False
        self._pump: asyncio.Task | None = None
        self._current: Path | None = None

    # -- control ------------------------------------------------------------

    def start(self, folder: Path, shuffle: bool = True) -> bool:
        """Begin the playlist. Returns False if the folder has no audio."""
        pygame = self.player._require()
        self.tracks = playlist(folder)
        if not self.tracks:
            self.log(f"no audio in {folder} — continuing without music")
            return False
        if shuffle:
            random.shuffle(self.tracks)
        self.index = 0
        self.playing = True
        self._load_current(pygame)
        if self._pump is None or self._pump.done():
            self._pump = asyncio.create_task(self._advance_loop())
        return True

    def _load_current(self, pygame) -> None:
        self._current = self.tracks[self.index % len(self.tracks)]
        pygame.mixer.music.load(str(self._current))
        pygame.mixer.music.set_volume(self.duck_volume if self._ducked else self.volume)
        pygame.mixer.music.play()
        self.log(f"music: {self._current.name}")

    async def _advance_loop(self) -> None:
        """Move to the next track when the current one ends."""
        pygame = self.player._require()
        try:
            while self.playing:
                await asyncio.sleep(0.5)
                if self.playing and not pygame.mixer.music.get_busy():
                    self.index += 1
                    self._load_current(pygame)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - music must never kill a routine
            self.log(f"music error: {e}")

    async def fade_to(self, target: float, ms: float = 250.0) -> None:
        """
        Slide the music volume rather than jumping it.

        An instant cut to silence sounds like a fault; a 250ms slide sounds
        deliberate. Short enough that the voice never starts before the music
        is out of its way.
        """
        if not self.playing:
            return
        pygame = self.player._require()
        start = pygame.mixer.music.get_volume()
        target = max(0.0, min(1.0, target))
        steps = max(1, int(ms / 25))
        for i in range(1, steps + 1):
            if not self.playing:
                return
            pygame.mixer.music.set_volume(start + (target - start) * (i / steps))
            await asyncio.sleep(0.025)

    async def duck(self) -> None:
        """Get out of the way of a voice line. Silent by default."""
        if not self.playing:
            return
        self._ducked = True
        await self.fade_to(self.duck_volume, 250)

    async def unduck(self) -> None:
        if not self.playing:
            return
        self._ducked = False
        await self.fade_to(self.volume, 450)

    def set_volume(self, v: float) -> None:
        self.volume = max(0.0, min(1.0, v))
        if self.playing and not self._ducked:
            self.player._require().mixer.music.set_volume(self.volume)

    async def fade_out(self, seconds: float = 8.0) -> None:
        """Fade to silence and stop. Never a hard cut."""
        if not self.playing:
            return
        pygame = self.player._require()
        self.log(f"music: fading out over {seconds:.0f}s")
        pygame.mixer.music.fadeout(int(seconds * 1000))
        self.playing = False
        if self._pump:
            self._pump.cancel()
            self._pump = None
        await asyncio.sleep(seconds)
        self._current = None

    def stop(self) -> None:
        """Immediate stop. Only the stop button should use this."""
        self.playing = False
        if self._pump:
            self._pump.cancel()
            self._pump = None
        try:
            self.player._require().mixer.music.stop()
        except AudioUnavailable:
            pass
        self._current = None

    @property
    def now_playing(self) -> str | None:
        return self._current.name if self._current else None
