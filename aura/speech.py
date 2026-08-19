"""
aura.speech — render every spoken line to disk, ahead of time.

No live TTS at 07:00. Lines are rendered when you edit them, and playback is
just reading a file. A TTS outage cannot stand between you and waking up.

That is also why a cloud TTS is fine here when a cloud *light* would not be:
the network dependency exists at render time and nowhere else.

Content-addressed — the cache key hashes (text, engine, voice, rate), so
editing one task re-renders one clip.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

DEFAULT_VOICE = "en-US-GuyNeural"


def key_for(text: str, engine: str, voice: str, rate: str) -> str:
    return hashlib.sha256(f"{engine}|{voice}|{rate}|{text}".encode()).hexdigest()[:16]


async def _render_edge(text: str, voice: str, rate: str, out: Path) -> None:
    import edge_tts

    await edge_tts.Communicate(text, voice, rate=rate).save(str(out))


def _render_sapi(text: str, out: Path) -> None:
    """Windows SAPI. Offline, always present, unmistakably robotic."""
    import pyttsx3

    engine = pyttsx3.init()
    engine.save_to_file(text, str(out))
    engine.runAndWait()
    engine.stop()


class SpeechLibrary:
    """
    Rendered speech, with your own recordings taking precedence.

    A clip you recorded yourself always beats the synthesised one — path_for()
    checks the custom directory first. That means a task can be re-worded
    without silently losing your recording, and deleting the recording falls
    straight back to TTS with no other change.
    """

    def __init__(self, out_dir: Path, engine: str = "edge",
                 voice: str = DEFAULT_VOICE, rate: str = "+0%",
                 custom_dir: Path | None = None):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.custom_dir = custom_dir or (out_dir.parent / "custom")
        self.custom_dir.mkdir(parents=True, exist_ok=True)
        self.engine = engine
        self.voice = voice
        self.rate = rate
        self.manifest_path = out_dir / "manifest.json"
        self.manifest = self._load()

    def _load(self) -> dict:
        if self.manifest_path.exists():
            try:
                return json.loads(self.manifest_path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return {}

    def _save(self) -> None:
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2), "utf-8")

    # Recording from the browser produces wav; an uploaded file could be
    # anything. Order is preference, not priority of correctness.
    CUSTOM_EXTS = (".wav", ".mp3", ".ogg", ".flac", ".m4a")

    def custom_path(self, line_id: str) -> Path | None:
        """Your own clip for this line, whatever format it arrived in."""
        for ext in self.CUSTOM_EXTS:
            p = self.custom_dir / f"{line_id}{ext}"
            if p.exists():
                return p
        return None

    def path_for(self, line_id: str) -> Path | None:
        """Your recording if there is one, otherwise the synthesised clip."""
        custom = self.custom_path(line_id)
        if custom:
            return custom
        e = self.manifest.get(line_id)
        if not e:
            return None
        p = self.out_dir / e["file"]
        return p if p.exists() else None

    def stale(self, specs: list[dict]) -> list[dict]:
        """Lines needing TTS. A line you recorded yourself is never stale."""
        out = []
        for spec in specs:
            if self.custom_path(spec["id"]):
                continue
            want = key_for(spec["text"], self.engine, self.voice, self.rate)
            have = self.manifest.get(spec["id"], {})
            if have.get("hash") != want or not self.path_for(spec["id"]):
                out.append(spec)
        return out

    def prune(self, specs: list[dict]) -> None:
        """Drop manifest entries for lines that no longer exist."""
        valid = {s["id"] for s in specs}
        for gone in [k for k in self.manifest if k not in valid]:
            f = self.out_dir / self.manifest[gone]["file"]
            f.unlink(missing_ok=True)
            del self.manifest[gone]
        self._save()

    async def render(self, specs: list[dict], force: bool = False, log=print) -> dict:
        todo = [s for s in specs if not self.custom_path(s["id"])] if force \
            else self.stale(specs)
        self.prune(specs)
        if not todo:
            log("all lines up to date.")
            return {"rendered": 0, "failed": 0}
        log(f"rendering {len(todo)} line(s) with '{self.engine}' ...")
        rendered = failed = 0
        for spec in todo:
            ext = "mp3" if self.engine == "edge" else "wav"
            fname = f"{spec['id']}.{ext}"
            dest = self.out_dir / fname
            try:
                if self.engine == "edge":
                    await _render_edge(spec["text"], self.voice, self.rate, dest)
                else:
                    _render_sapi(spec["text"], dest)
                if not dest.exists() or dest.stat().st_size == 0:
                    raise RuntimeError("engine produced no audio")
                self.manifest[spec["id"]] = {
                    "file": fname,
                    "hash": key_for(spec["text"], self.engine, self.voice, self.rate),
                    "text": spec["text"],
                }
                rendered += 1
                log(f"  ok  {spec['id']:10s} {spec['text'][:52]}")
            except Exception as e:  # noqa: BLE001 - report and carry on
                failed += 1
                log(f"  !!  {spec['id']:10s} {type(e).__name__}: {e}")
        self._save()
        if failed and self.engine == "edge":
            log("edge-tts failures are almost always network. For offline, "
                "switch the engine to 'sapi'.")
        return {"rendered": rendered, "failed": failed}
