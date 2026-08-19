#!/usr/bin/env python3
"""
aura — local control panel.

    python aura_web.py        then open http://127.0.0.1:8770

Five things and nothing else: wake time, bedtime, morning tasks, evening tasks,
and the two audio libraries. Everything else has a defensible default.

Binds to 127.0.0.1 only.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import subprocess
import sys
import tomllib
import webbrowser
from contextlib import asynccontextmanager, suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI, File, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from aura import settings as S  # noqa: E402
from aura.audio import AUDIO_EXTS, AudioUnavailable, MusicPlayer, Player, playlist  # noqa: E402
from aura.flow import MorningFlow, NightFlow  # noqa: E402
from aura.light import AuraLight, BulbUnreachable, EnvironmentBroken  # noqa: E402
from aura.scheduler import Scheduler  # noqa: E402
from aura.speech import SpeechLibrary  # noqa: E402

HERE = Path(__file__).parent
TTS_DIR = HERE / "audio" / "voice"
MOTIVATION_DIR = HERE / "audio" / "motivational"
PIANO_DIR = HERE / "audio" / "piano"
CUSTOM_DIR = HERE / "audio" / "custom"      # your own recordings, per line
NAG_DIR = HERE / "audio" / "nags"           # rotating get-up recordings
KEEPALIVE_WAV = HERE / "audio" / "keepalive.wav"
SETTINGS_PATH = HERE / "settings.json"
FIRED_PATH = HERE / "last_fired.json"
PORT = 8770
NOW_URL = f"http://127.0.0.1:{PORT}/now"

@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="aura", lifespan=lifespan)
player = Player()
music = MusicPlayer(player)
state: dict = {"flow": None, "task": None, "last": None, "events": []}

LIBRARIES = {"motivational": MOTIVATION_DIR, "piano": PIANO_DIR}

# Constructed early because the lifespan handler needs it; the real callables
# are attached further down, once they exist.
scheduler = Scheduler(settings_fn=lambda: None, start_fn=None,
                      is_busy_fn=lambda: False, state_path=HERE / "last_fired.json")


def cfg() -> dict:
    """
    Load hardware config, creating it from the example on a fresh clone.

    A clone has aura.toml.example but no aura.toml, because aura.toml holds
    your bulb's address and is git-ignored. Rather than crash on first run,
    copy the example so the app starts and can tell you what to fix.
    """
    path = HERE / "aura.toml"
    if not path.exists():
        example = HERE / "aura.toml.example"
        if example.exists():
            shutil.copyfile(example, path)
            print(f"created {path.name} from the example — "
                  f"set your bulb IP (python aura_web.py --discover)")
    with path.open("rb") as f:
        return tomllib.load(f)


def note(msg: str) -> None:
    state["events"].append(msg)
    del state["events"][:-200]
    print(msg, flush=True)


def settings() -> S.Settings:
    return S.load(SETTINGS_PATH)


def speech_lib() -> SpeechLibrary:
    v = cfg().get("voice", {})
    return SpeechLibrary(TTS_DIR, engine=v.get("engine", "edge"),
                         voice=v.get("name", "en-US-GuyNeural"),
                         rate=v.get("rate", "+0%"), custom_dir=CUSTOM_DIR)


def fail(e: Exception) -> JSONResponse:
    kind = {
        EnvironmentBroken: "environment",
        BulbUnreachable: "can't reach the bulb — is the lamp switched on?",
        AudioUnavailable: "audio",
        FileNotFoundError: "missing file",
    }.get(type(e), type(e).__name__)
    note(f"error [{kind}]: {e}")
    return JSONResponse({"ok": False, "error": f"{kind}: {e}"}, status_code=400)


class RunReq(BaseModel):
    which: str = "morning"
    speed: float = 1.0
    sim: bool = False
    # Demo overrides. None means "use the real configured value".
    ramp_minutes: float | None = None   # shorten the sunrise without speeding
    hold_s: float | None = None         # shorten the wait-until-bedtime hold


# --- launching the control screen ------------------------------------------


def open_control_screen() -> None:
    """
    Pop the control screen up on its own.

    Chrome and Edge both support --app=, which gives a clean window with no
    tabs, no address bar and no bookmarks — the point being that at 6:30am you
    should see the instruction, not a browser. Falls back to a normal tab if
    neither is installed.
    """
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for exe in candidates:
        if exe and Path(exe).exists():
            try:
                subprocess.Popen([exe, f"--app={NOW_URL}",
                                  "--window-size=900,640", "--new-window"])
                note("control screen opened")
                return
            except OSError:
                continue
    try:
        webbrowser.open(NOW_URL)
        note("control screen opened in the default browser")
    except Exception as e:  # noqa: BLE001 - never let this break a routine
        note(f"could not open the control screen: {e}")


# --- settings --------------------------------------------------------------


@app.get("/api/settings")
async def get_settings():
    s = settings()
    lib = speech_lib()
    specs = S.line_specs(s)
    return {
        "ok": True,
        "settings": s.to_dict(),
        "schedule": s.schedule_summary(),
        "unrendered": [x["id"] for x in lib.stale(specs)],
        "libraries": {k: [p.name for p in playlist(v)] for k, v in LIBRARIES.items()},
        "recorded": [p.stem for p in CUSTOM_DIR.glob("*.wav")],
        "nags": [p.name for p in playlist(NAG_DIR)],
        "fixed": [{"id": k, "text": v} for k, v in S.FIXED_LINES.items()],
    }


@app.put("/api/settings")
async def put_settings(body: dict):
    try:
        s = S.Settings.from_dict(body)
        S.save(SETTINGS_PATH, s)
        note("settings saved")
        return {"ok": True, "schedule": s.schedule_summary(),
                "unrendered": [x["id"] for x in speech_lib().stale(S.line_specs(s))]}
    except Exception as e:  # noqa: BLE001
        return fail(e)


@app.post("/api/render")
async def render(force: bool = False):
    try:
        s = settings()
        result = await speech_lib().render(S.line_specs(s), force=force, log=note)
        return {"ok": result["failed"] == 0, **result}
    except Exception as e:  # noqa: BLE001
        return fail(e)


# --- audio libraries -------------------------------------------------------


@app.post("/api/library/{name}")
async def upload(name: str, files: list[UploadFile] = File(...)):
    if name not in LIBRARIES:
        return JSONResponse({"ok": False, "error": "unknown library"}, 400)
    dest = LIBRARIES[name]
    dest.mkdir(parents=True, exist_ok=True)
    saved, skipped = [], []
    for f in files:
        suffix = Path(f.filename or "").suffix.lower()
        if suffix not in AUDIO_EXTS:
            skipped.append(f.filename)
            continue
        target = dest / Path(f.filename).name
        with target.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(target.name)
    note(f"{name}: added {len(saved)} file(s)")
    return {"ok": True, "saved": saved, "skipped": skipped}


@app.delete("/api/library/{name}/{filename}")
async def remove(name: str, filename: str):
    if name not in LIBRARIES:
        return JSONResponse({"ok": False, "error": "unknown library"}, 400)
    target = LIBRARIES[name] / Path(filename).name  # basename only, no traversal
    if target.exists():
        target.unlink()
        note(f"{name}: removed {target.name}")
    return {"ok": True}


# --- your own recordings ---------------------------------------------------
#
# The browser records raw PCM via Web Audio and encodes WAV client-side, so
# what arrives here is already a format SDL_mixer plays natively. Recording
# straight from MediaRecorder would give webm/opus, which pygame cannot open
# and which would need ffmpeg to convert.


def check_wav(path: Path) -> tuple[bool, str]:
    """
    Confirm a WAV is actually playable before anything tries to play it.

    SDL crashes the whole process on a malformed or empty file rather than
    raising, so every recording is parsed with the stdlib `wave` module first.
    A rejected upload is a message; an unchecked one was a dead server.
    """
    try:
        import wave

        with wave.open(str(path), "rb") as w:
            frames, rate = w.getnframes(), w.getframerate()
            ch, width = w.getnchannels(), w.getsampwidth()
        if frames == 0:
            return False, "the recording is empty"
        secs = frames / float(rate or 1)
        if secs < 0.25:
            return False, f"too short ({secs:.2f}s) - hold the button a little longer"
        if width not in (1, 2, 4) or ch not in (1, 2) or not (8000 <= rate <= 192000):
            return False, f"unsupported format ({rate}Hz, {ch}ch, {width * 8}-bit)"
        return True, f"{secs:.1f}s, {rate}Hz, {ch}ch, {width * 8}-bit"
    except Exception as e:  # noqa: BLE001
        return False, f"not a readable WAV ({e})"


@app.post("/api/record/{line_id}")
async def record_line(line_id: str, file: UploadFile = File(...)):
    """Replace one line with your own voice. Overrides the synthesised clip."""
    safe = "".join(ch for ch in line_id if ch.isalnum() or ch in "_-")
    if not safe:
        return JSONResponse({"ok": False, "error": "bad line id"}, 400)
    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CUSTOM_DIR / f".{safe}.part"
    with tmp.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    ok, detail = check_wav(tmp)
    if not ok:
        tmp.unlink(missing_ok=True)
        note(f"rejected recording for {safe}: {detail}")
        return JSONResponse({"ok": False, "error": detail}, 400)
    target = CUSTOM_DIR / f"{safe}.wav"
    tmp.replace(target)
    note(f"recorded {safe} ({detail})")
    return {"ok": True, "id": safe, "detail": detail}


@app.delete("/api/record/{line_id}")
async def unrecord_line(line_id: str):
    """Delete a recording. The line falls back to TTS with no other change."""
    target = CUSTOM_DIR / f"{Path(line_id).name}.wav"
    if target.exists():
        target.unlink()
        note(f"removed recording {line_id}")
    return {"ok": True}


@app.post("/api/nags")
async def add_nag(file: UploadFile = File(...)):
    """Append another get-up reminder. They play in rotation, not at random."""
    NAG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = NAG_DIR / ".upload.part"
    with tmp.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    ok, detail = check_wav(tmp)
    if not ok:
        tmp.unlink(missing_ok=True)
        note(f"rejected reminder: {detail}")
        return JSONResponse({"ok": False, "error": detail}, 400)
    n = 1
    while (NAG_DIR / f"nag{n:02d}.wav").exists():
        n += 1
    target = NAG_DIR / f"nag{n:02d}.wav"
    tmp.replace(target)
    note(f"added get-up reminder {target.name} ({detail})")
    return {"ok": True, "name": target.name, "detail": detail}


@app.delete("/api/nags/{filename}")
async def del_nag(filename: str):
    target = NAG_DIR / Path(filename).name
    if target.exists():
        target.unlink()
        note(f"removed {target.name}")
    return {"ok": True}


@app.get("/api/clip/{kind}/{name}")
async def clip(kind: str, name: str):
    """
    Serve a clip so the BROWSER plays it, not SDL.

    Auditioning used to go through pygame, which meant one malformed file could
    take down the whole server. The browser has its own decoder in its own
    process and plays through the same Windows default output — so you still
    hear it on the soundbar, with nothing at stake if a file is bad.
    """
    if kind == "line":
        p = speech_lib().path_for(Path(name).name)
    elif kind == "nag":
        p = NAG_DIR / Path(name).name
    else:
        return JSONResponse({"ok": False, "error": "unknown kind"}, 400)
    if not p or not Path(p).exists():
        return JSONResponse({"ok": False, "error": "nothing recorded yet"}, 404)
    return FileResponse(str(p))


# --- light ------------------------------------------------------------------


class LightReq(BaseModel):
    pct: int | None = None       # None = off
    kelvin: int = 2700


@app.post("/api/light")
async def set_light(req: LightReq):
    """
    Manual light control, mostly so there is always a way back to a known state.

    A routine that was stopped halfway leaves the lamp at whatever brightness
    it had reached; this is the reset.
    """
    try:
        lt = AuraLight(ip=cfg()["bulb"]["ip"], log=note)
        await lt.connect(retries=2, delay_s=1)
        if req.pct is None:
            await lt.off()
            note("light off")
        else:
            await lt.apply(max(1, min(100, req.pct)), req.kelvin)
            note(f"light {req.pct}%")
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return fail(e)


# --- running ---------------------------------------------------------------


async def begin(which: str, speed: float = 1.0, sim: bool = False,
                ramp_skip_s: float = 0.0, ramp_minutes: float | None = None,
                hold_s: float | None = None) -> dict:
    """Start a routine. Shared by the Try-it buttons and the scheduler."""
    task = state["task"]
    if task and not task.done():
        return {"ok": False, "error": "already running"}
    req = RunReq(which=which, speed=speed, sim=sim)
    try:
        s = settings()
        if ramp_minutes:
            # A demo wants a short sunrise at NORMAL pace — everything after it
            # (speech, nags, clicks) should feel exactly like the real thing.
            s = dataclasses.replace(s, ramp_minutes=ramp_minutes)
        lib = speech_lib()
        missing = [x["id"] for x in lib.stale(S.line_specs(s))]
        if missing:
            return {"ok": False,
                    "error": f"{len(missing)} line(s) have no audio yet — "
                             f"press Prepare voice"}
        p = m = lt = None
        if not req.sim:
            player.start(log=note)
            with suppress(Exception):
                player.keepalive_start(KEEPALIVE_WAV)
            p, m = player, music
            music.set_volume(s.music_volume)
            music.duck_volume = s.duck_volume
            lt = AuraLight(ip=cfg()["bulb"]["ip"], log=note)
            await lt.connect(retries=2, delay_s=1)
        cls = MorningFlow if req.which == "morning" else NightFlow
        kw = ({"ramp_skip_s": ramp_skip_s} if req.which == "morning"
              else {"hold_s": hold_s})
        flow = cls(s, lib, p, m, lt, log=note, speed=req.speed, sim=req.sim,
                   motivation_dir=MOTIVATION_DIR, piano_dir=PIANO_DIR,
                   nag_dir=NAG_DIR, **kw)
    except Exception as e:  # noqa: BLE001
        note(f"could not start {which}: {e}")
        return {"ok": False, "error": str(e)}

    async def go():
        try:
            state["last"] = await flow.run()
        except asyncio.CancelledError:
            state["last"] = "cancelled"
            raise
        except Exception as e:  # noqa: BLE001
            state["last"] = f"error: {e}"
            note(f"routine error: {e}")

    state["flow"] = flow
    state["last"] = None
    note(f"running {req.which} at x{req.speed:g}" + (" [sim]" if req.sim else ""))
    state["task"] = asyncio.create_task(go())
    return {"ok": True}


@app.post("/api/run")
async def run(req: RunReq):
    r = await begin(req.which, req.speed, req.sim,
                    ramp_minutes=req.ramp_minutes, hold_s=req.hold_s)
    if not r.get("ok"):
        return JSONResponse(r, 409 if "already" in r.get("error", "") else 400)
    return r


@app.post("/api/click")
async def click():
    f = state["flow"]
    if not f:
        return JSONResponse({"ok": False, "error": "nothing running"}, 400)
    f.click()
    return {"ok": True}


@app.post("/api/stop")
async def stop():
    f = state["flow"]
    if f:
        f.stop()
    t = state["task"]
    if t and not t.done():
        await asyncio.sleep(0.3)
        if not t.done():
            t.cancel()
            with suppress(asyncio.CancelledError):
                await t
    with suppress(Exception):
        music.stop()
    note("stopped")
    return {"ok": True}


# --- scheduler -------------------------------------------------------------


async def _scheduler_start(which: str, ramp_skip_s: float) -> None:
    r = await begin(which, speed=1.0, sim=False, ramp_skip_s=ramp_skip_s)
    if r.get("ok"):
        open_control_screen()
    else:
        note(f"scheduled {which} did not start: {r.get('error')}")


scheduler.settings_fn = settings
scheduler.start_fn = _scheduler_start
scheduler.is_busy_fn = lambda: bool(state["task"] and not state["task"].done())
scheduler.state_path = FIRED_PATH
scheduler.log = note


# (scheduler starts in the lifespan handler at the bottom of this file)


@app.get("/api/schedule")
async def api_schedule():
    return {"ok": True, "enabled": scheduler.enabled, "next": scheduler.next_runs()}


@app.post("/api/schedule")
async def api_schedule_set(body: dict):
    scheduler.enabled = bool(body.get("enabled", True))
    note(f"automatic mode {'on' if scheduler.enabled else 'off'}")
    return {"ok": True, "enabled": scheduler.enabled}


@app.post("/api/open-control")
async def api_open_control():
    open_control_screen()
    return {"ok": True}


@app.get("/api/status")
async def status():
    f, t = state["flow"], state["task"]
    return {
        "ok": True,
        "active": bool(t and not t.done()),
        "flow": f.snapshot() if f else None,
        "last": state["last"],
        "events": state["events"][-20:],
        "next": scheduler.next_runs(),
        "auto": scheduler.enabled,
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return PAGE


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>aura</title>
<style>
:root{
  --bg:#f5f5f7; --card:#fff; --fg:#1d1d1f; --dim:#6e6e73; --line:#e3e3e6;
  --accent:#0071e3; --ok:#1d8a4e; --bad:#d13438; --shadow:0 1px 3px rgba(0,0,0,.06);
}
@media(prefers-color-scheme:dark){:root{
  --bg:#000; --card:#1c1c1e; --fg:#f5f5f7; --dim:#8e8e93; --line:#2c2c2e;
  --accent:#0a84ff; --ok:#30d158; --bad:#ff453a; --shadow:none;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:16px/1.47 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:680px;margin:0 auto;padding:56px 24px 96px}
h1{font-size:30px;font-weight:600;letter-spacing:-.02em;margin:0 0 4px}
.sub{color:var(--dim);font-size:15px;margin-bottom:40px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;
  padding:22px 24px;margin-bottom:18px;box-shadow:var(--shadow)}
h2{font-size:13px;font-weight:600;letter-spacing:.02em;color:var(--dim);
  text-transform:uppercase;margin:0 0 18px}
.times{display:flex;gap:28px;flex-wrap:wrap}
.tf{flex:1;min-width:150px}
.tf label{display:block;font-size:13px;color:var(--dim);margin-bottom:6px}
input[type=time]{font-size:30px;font-weight:300;letter-spacing:-.02em;
  background:none;border:none;color:var(--fg);padding:0;width:100%;
  font-family:inherit}
input[type=time]::-webkit-calendar-picker-indicator{opacity:.35;cursor:pointer}
.sched{margin-top:20px;padding-top:18px;border-top:1px solid var(--line);
  font-size:14px;color:var(--dim);line-height:1.9}
.sched b{color:var(--fg);font-weight:500;font-variant-numeric:tabular-nums}
.task{display:flex;align-items:center;gap:10px;padding:9px 0;
  border-bottom:1px solid var(--line)}
.task:last-of-type{border-bottom:none}
.task input[type=text]{flex:1;font-size:16px;background:none;border:none;
  color:var(--fg);font-family:inherit;padding:4px 0;min-width:0}
.task input[type=number]{width:56px;font-size:15px;text-align:right;
  background:none;border:none;color:var(--dim);font-family:inherit}
.num{width:66px;font-size:15px;text-align:right;background:none;
  border:1px solid var(--line);border-radius:8px;padding:7px;color:var(--fg);
  font-family:inherit}
.task .mins{font-size:13px;color:var(--dim)}
input:focus{outline:none}
input[type=text]:focus,input[type=number]:focus{border-bottom:2px solid var(--accent)}
.rec{background:none;border:1px solid var(--line);color:var(--dim);cursor:pointer;
  border-radius:999px;width:30px;height:30px;font-size:12px;line-height:1;
  display:inline-flex;align-items:center;justify-content:center;flex:none;
  font-family:inherit;padding:0}
.rec:hover{border-color:var(--bad);color:var(--bad)}
.rec.on{background:var(--bad);border-color:var(--bad);color:#fff;
  animation:pulse 1.1s ease-in-out infinite}
.rec.has{border-color:var(--ok);color:var(--ok)}
@keyframes pulse{50%{opacity:.55}}
.play{background:none;border:none;color:var(--dim);cursor:pointer;font-size:14px;
  padding:2px 5px;border-radius:6px;flex:none;font-family:inherit}
.play:hover{color:var(--accent)}
.x{background:none;border:none;color:var(--dim);cursor:pointer;font-size:19px;
  line-height:1;padding:2px 5px;border-radius:6px}
.x:hover{color:var(--bad);background:var(--bg)}
.add{background:none;border:none;color:var(--accent);cursor:pointer;
  font-size:15px;padding:12px 0 0;font-family:inherit}
button.b{background:var(--accent);color:#fff;border:none;border-radius:10px;
  padding:11px 20px;font-size:15px;font-weight:500;cursor:pointer;font-family:inherit}
button.b:disabled{opacity:.4;cursor:default}
button.g{background:none;color:var(--accent);border:1px solid var(--line)}
button.d{background:none;color:var(--bad);border:1px solid var(--line)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.drop{border:1.5px dashed var(--line);border-radius:12px;padding:26px;
  text-align:center;color:var(--dim);cursor:pointer;font-size:14px;
  transition:border-color .15s,background .15s}
.drop:hover,.drop.over{border-color:var(--accent);color:var(--accent)}
.files{margin-top:12px;font-size:14px}
.file{display:flex;align-items:center;gap:8px;padding:6px 0;color:var(--dim)}
.file span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hint{font-size:13px;color:var(--dim);margin-top:12px;line-height:1.6}
/* running */
#run{position:fixed;inset:0;background:var(--bg);display:none;
  flex-direction:column;align-items:center;justify-content:center;
  padding:40px;text-align:center;z-index:50}
#run.on{display:flex}
.phase{font-size:12px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--dim);margin-bottom:26px}
.big{font-size:38px;font-weight:300;letter-spacing:-.02em;line-height:1.25;
  max-width:16em;margin-bottom:40px}
.cbtn{background:var(--accent);color:#fff;border:none;border-radius:999px;
  padding:22px 62px;font-size:20px;font-weight:500;cursor:pointer;
  font-family:inherit;letter-spacing:.02em}
.cbtn:disabled{opacity:.18;cursor:default}
.meta{margin-top:34px;font-size:13px;color:var(--dim);line-height:1.9}
#runlog{margin-top:26px;font:11.5px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace;
  color:var(--dim);max-height:150px;overflow:auto;text-align:left;
  max-width:560px;width:100%;white-space:pre-wrap}
#toast{position:fixed;bottom:26px;left:50%;transform:translateX(-50%);
  background:var(--fg);color:var(--bg);padding:11px 20px;border-radius:10px;
  font-size:14px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:60}
#toast.on{opacity:1}
</style></head><body>

<div class="wrap">
  <h1>aura</h1>
  <div class="sub" id="sub">&nbsp;</div>

  <div class="card">
    <h2>Schedule</h2>
    <div class="times">
      <div class="tf"><label>Wake up</label>
        <input type="time" id="wake" onchange="mark()"></div>
      <div class="tf"><label>Asleep by</label>
        <input type="time" id="bed" onchange="mark()"></div>
    </div>
    <div class="sched" id="sched"></div>
  </div>

  <div class="card">
    <h2>Morning tasks</h2>
    <div id="mtasks"></div>
    <button class="add" onclick="addTask('morning')">+ Add task</button>
  </div>

  <div class="card">
    <h2>Evening tasks</h2>
    <div id="etasks"></div>
    <button class="add" onclick="addTask('evening')">+ Add task</button>
    <div class="hint">Durations decide when the wind-down starts, so that the
      last task finishes with time to spare before you want to be asleep.</div>
  </div>

  <div class="card">
    <h2>Get-up reminders</h2>
    <div class="hint" style="margin:0 0 14px">
      Recorded in your own voice, played in rotation every 10 seconds until you
      get up. Record several so it doesn't become one sound you sleep through.</div>
    <div id="nags"></div>
    <div class="row" style="margin-top:12px">
      <button class="rec" id="nagrec" onclick="toggleNag()" title="Record">●</button>
      <span class="mins" id="nagstate" style="color:var(--dim);font-size:13px">
        Record another reminder</span>
    </div>
  </div>

  <div class="card">
    <h2>Spoken lines</h2>
    <div class="hint" style="margin:0 0 14px">
      Synthesised by default. Record any of them in your own voice and yours
      wins — delete the recording to go back.</div>
    <div id="fixed"></div>
  </div>

  <div class="card">
    <h2>Motivational audio — morning</h2>
    <div class="drop" id="d-motivational">Drop audio here, or click to choose</div>
    <div class="files" id="f-motivational"></div>
  </div>

  <div class="card">
    <h2>Piano — evening</h2>
    <div class="drop" id="d-piano">Drop audio here, or click to choose</div>
    <div class="files" id="f-piano"></div>
  </div>

  <div class="card">
    <h2>Light</h2>
    <div class="row">
      <button class="b g" onclick="light(null)">Turn off</button>
      <button class="b g" onclick="light(1)">1% — bedtime level</button>
      <button class="b g" onclick="light(60)">60%</button>
      <button class="b g" onclick="light(100)">Full</button>
    </div>
    <div class="hint">Somewhere to get back to a known state — a routine you
      stopped halfway leaves the lamp wherever it had reached.</div>
  </div>

  <div class="card">
    <h2>Automatic</h2>
    <div class="row">
      <label style="font-size:15px;flex:1">
        <input type="checkbox" id="auto" checked onchange="setAuto()">
        Run by itself every day</label>
    </div>
    <div class="sched" id="next" style="border:none;padding-top:14px;margin-top:6px"></div>
    <div class="hint">The control screen opens on its own when a routine
      starts. Leave this app running — see the README for starting it at logon.</div>
  </div>

  <div class="card">
    <h2>Try it now</h2>
    <div class="row">
      <button class="b" onclick="save()">Save</button>
      <button class="b g" onclick="record()" id="prep">Prepare voice</button>
      <button class="b g" onclick="post('/api/open-control')">Open control screen</button>
    </div>

    <div class="row" style="margin-top:20px">
      <label style="font-size:14px;flex:1">Sunrise length for this run</label>
      <input type="number" id="ramp" value="1" min="0.2" step="0.5" class="num">
      <span class="mins">min</span>
    </div>
    <div class="row">
      <label style="font-size:14px;flex:1">Hold at 1% before lights out</label>
      <input type="number" id="hold" value="1" min="0.1" step="0.5" class="num">
      <span class="mins">min</span>
    </div>
    <div class="hint" style="margin-top:4px">
      Everything else runs at real pace — the pauses, the 10-second reminders
      and the clicks all feel exactly like the real thing.</div>

    <div class="row" style="margin-top:18px">
      <button class="b" onclick="demo('morning')">Run morning</button>
      <button class="b" onclick="demo('night')">Run evening</button>
      <span style="flex:1"></span>
      <label style="font-size:13px;color:var(--dim)">
        <input type="checkbox" id="sim"> silent</label>
      <label style="font-size:13px;color:var(--dim)">×</label>
      <input type="number" id="speed" value="1" min="1" step="1" class="num"
        title="Leave at 1 for a realistic demo">
    </div>
    <div class="hint" id="rhint"></div>
  </div>
</div>

<div id="run">
  <div class="phase" id="rphase"></div>
  <div class="big" id="rtext"></div>
  <button class="cbtn" id="rbtn" onclick="post('/api/click')" disabled>—</button>
  <div class="meta" id="rmeta"></div>
  <div id="runlog"></div>
  <div style="margin-top:26px">
    <button class="b d" onclick="post('/api/stop')">Stop</button></div>
</div>

<div id="toast"></div>
<script>
const $=id=>document.getElementById(id);
let ST=null, DIRTY=false;
function toast(m,bad){const t=$('toast');t.textContent=m;
  t.style.background=bad?'var(--bad)':'var(--fg)';
  t.style.color=bad?'#fff':'var(--bg)';t.classList.add('on');
  clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('on'),2600);}
async function post(u,b){const r=await fetch(u,{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});
  const j=await r.json(); if(!j.ok)toast(j.error||'failed',true); return j;}
function mark(){DIRTY=true;$('sub').textContent='Unsaved changes';}

// ---- microphone -> 16-bit PCM WAV, encoded in the browser ----------------
// MediaRecorder would give webm/opus, which SDL_mixer cannot open and which
// would need ffmpeg server-side. Raw PCM via Web Audio avoids all of that.
let REC=null;
async function recStart(){
  const stream=await navigator.mediaDevices.getUserMedia({audio:{
    echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
  const ctx=new (window.AudioContext||window.webkitAudioContext)();
  const src=ctx.createMediaStreamSource(stream);
  const node=ctx.createScriptProcessor(4096,1,1);
  const chunks=[];
  node.onaudioprocess=e=>chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  src.connect(node); node.connect(ctx.destination);
  REC={stream,ctx,src,node,chunks,rate:ctx.sampleRate};
}
function recStop(){
  if(!REC)return null;
  const {stream,ctx,src,node,chunks,rate}=REC; REC=null;
  node.disconnect(); src.disconnect(); stream.getTracks().forEach(t=>t.stop());
  ctx.close();
  let len=0; for(const c of chunks) len+=c.length;
  const pcm=new Float32Array(len); let o=0;
  for(const c of chunks){pcm.set(c,o); o+=c.length;}
  return wav(pcm,rate);
}
function wav(pcm,rate){
  const buf=new ArrayBuffer(44+pcm.length*2), v=new DataView(buf);
  const str=(off,s)=>{for(let i=0;i<s.length;i++)v.setUint8(off+i,s.charCodeAt(i));};
  str(0,'RIFF'); v.setUint32(4,36+pcm.length*2,true); str(8,'WAVE');
  str(12,'fmt '); v.setUint32(16,16,true); v.setUint16(20,1,true);
  v.setUint16(22,1,true); v.setUint32(24,rate,true);
  v.setUint32(28,rate*2,true); v.setUint16(32,2,true); v.setUint16(34,16,true);
  str(36,'data'); v.setUint32(40,pcm.length*2,true);
  for(let i=0;i<pcm.length;i++){
    const x=Math.max(-1,Math.min(1,pcm[i]));
    v.setInt16(44+i*2, x<0?x*0x8000:x*0x7fff, true);
  }
  return new Blob([buf],{type:'audio/wav'});
}
async function sendWav(url,blob,field){
  const fd=new FormData(); fd.append(field,blob,'clip.wav');
  const r=await fetch(url,{method:'POST',body:fd}); return r.json();
}

let RECTARGET=null;
async function toggleRec(id,btn){
  if(RECTARGET&&RECTARGET!==id)return;
  if(RECTARGET===id){
    const blob=recStop(); RECTARGET=null;
    btn.classList.remove('on');
    if(!blob||blob.size<8000){toast('Too short — hold it a bit longer',true);return;}
    const j=await sendWav('/api/record/'+id,blob,'file');
    toast(j.ok?('Recorded · '+(j.detail||'')):j.error,!j.ok); load(); return;
  }
  try{ await recStart(); }catch(e){ toast('Microphone blocked',true); return; }
  RECTARGET=id; btn.classList.add('on'); toast('Recording — click again to stop');
}
async function toggleNag(){
  const btn=$('nagrec');
  if(RECTARGET==='__nag'){
    const blob=recStop(); RECTARGET=null; btn.classList.remove('on');
    $('nagstate').textContent='Record another reminder';
    if(!blob||blob.size<8000){toast('Too short — hold it a bit longer',true);return;}
    const j=await sendWav('/api/nags',blob,'file');
    toast(j.ok?('Reminder added · '+(j.detail||'')):j.error,!j.ok); load(); return;
  }
  try{ await recStart(); }catch(e){ toast('Microphone blocked',true); return; }
  RECTARGET='__nag'; btn.classList.add('on');
  $('nagstate').textContent='Recording — click to stop';
}
async function unrec(id){await fetch('/api/record/'+id,{method:'DELETE'});load();}
async function delNag(f){await fetch('/api/nags/'+encodeURIComponent(f),
  {method:'DELETE'});load();}
// Playback happens in the browser, not through pygame — see /api/clip.
let AUD=null;
function prev(kind,name){
  if(AUD){AUD.pause();AUD=null;}
  AUD=new Audio(`/api/clip/${kind}/${encodeURIComponent(name)}?t=`+Date.now());
  AUD.play().catch(e=>toast('Could not play: '+e.message,true));
}

function recBtn(id){
  const has=(ST.recorded||[]).includes(id);
  return `<button class="rec ${has?'has':''}" onclick="toggleRec('${id}',this)"
     title="${has?'Re-record':'Record in your voice'}">●</button>`
   +(has?`<button class="play" onclick="prev('line','${id}')" title="Play">▶</button>
          <button class="x" onclick="unrec('${id}')" title="Use synthesised voice">×</button>`
        :'');
}

function taskRow(t,kind,i){
  const mins=kind==='evening'
    ?`<input type="number" min="1" step="1" value="${t.minutes}"
        oninput="ST.settings.${kind}_tasks[${i}].minutes=+this.value;mark()">
      <span class="mins">min</span>`:'';
  return `<div class="task">
    <input type="text" value="${(t.text||'').replace(/"/g,'&quot;')}"
      oninput="ST.settings.${kind}_tasks[${i}].text=this.value;mark()"
      placeholder="What do you need to do?">
    ${mins}
    ${recBtn(kind==='morning'?'m'+i:'e'+i)}
    <button class="x" onclick="delTask('${kind}',${i})">×</button></div>`;
}
function drawTasks(){
  $('mtasks').innerHTML=ST.settings.morning_tasks.map((t,i)=>taskRow(t,'morning',i)).join('');
  $('etasks').innerHTML=ST.settings.evening_tasks.map((t,i)=>taskRow(t,'evening',i)).join('');
}
function addTask(kind){
  ST.settings[kind+'_tasks'].push(kind==='evening'?{text:'',minutes:3}:{text:'',minutes:3});
  drawTasks();mark();}
function delTask(kind,i){ST.settings[kind+'_tasks'].splice(i,1);drawTasks();mark();}

function drawSchedule(sc){
  $('sched').innerHTML=
    `Sunrise starts <b>${sc.ramp_start}</b>, full brightness at <b>${sc.wake}</b>.<br>`
   +`Wind-down starts <b>${sc.night_start}</b> — `
   +`${sc.evening_total_minutes} min of tasks, then <b>${sc.buffer_minutes}</b> min `
   +`of near-darkness before lights out at <b>${sc.bedtime}</b>.`;
}
function drawNags(){
  const ns=ST.nags||[];
  $('nags').innerHTML=ns.length?ns.map((f,i)=>
    `<div class="task"><span style="flex:1;color:var(--dim);font-size:15px">
       Reminder ${i+1}</span>
     <button class="play" onclick="prev('nag','${f}')">▶</button>
     <button class="x" onclick="delNag('${f}')">×</button></div>`).join('')
    :`<div class="file"><span>None yet — the synthesised "Get up." is used.</span></div>`;
}
function drawFixed(){
  $('fixed').innerHTML=(ST.fixed||[]).map(l=>
    `<div class="task"><span style="flex:1;font-size:15px">${l.text}</span>
     ${recBtn(l.id)}</div>`).join('');
}
function drawFiles(){
  for(const k of ['motivational','piano']){
    const fs=ST.libraries[k]||[];
    $('f-'+k).innerHTML=fs.length?fs.map(f=>
      `<div class="file"><span>${f.replace(/</g,'&lt;')}</span>
       <button class="x" onclick="delFile('${k}','${encodeURIComponent(f)}')">×</button></div>`
      ).join(''):'<div class="file"><span>No files yet.</span></div>';
  }
}
async function delFile(k,f){await fetch(`/api/library/${k}/${f}`,{method:'DELETE'});load();}

async function load(){
  ST=await (await fetch('/api/settings')).json();
  $('wake').value=ST.settings.wake_time; $('bed').value=ST.settings.bedtime;
  drawTasks(); drawSchedule(ST.schedule); drawFiles(); drawNags(); drawFixed();
  DIRTY=false;
  $('sub').textContent=ST.unrendered.length
    ? `${ST.unrendered.length} line(s) not recorded yet`
    : 'Everything recorded and ready.';
  $('rhint').textContent=ST.unrendered.length
    ? 'Press Prepare voice — it only fills in lines you have not recorded yourself.'
    : '';
  pollNext();
}
async function save(){
  ST.settings.wake_time=$('wake').value; ST.settings.bedtime=$('bed').value;
  const r=await fetch('/api/settings',{method:'PUT',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(ST.settings)});
  const j=await r.json();
  if(j.ok){drawSchedule(j.schedule);DIRTY=false;
    toast(j.unrendered.length?`Saved — ${j.unrendered.length} line(s) need recording`:'Saved');
    load();} else toast(j.error,true);
}
async function record(){
  toast('Generating speech for lines you have not recorded…');
  const j=await post('/api/render');
  if(j.ok)toast(j.rendered?`Prepared ${j.rendered} line(s)`:'Everything ready');
  load();
}
async function setAuto(){await post('/api/schedule',{enabled:$('auto').checked});}
async function light(pct){
  const j=await post('/api/light',{pct:pct,kelvin:pct===null?2700:(pct<=1?2200:2700)});
  if(j.ok)toast(pct===null?'Light off':`Light ${pct}%`);
}
async function pollNext(){
  try{
    const j=await (await fetch('/api/schedule')).json();
    $('auto').checked=j.enabled;
    $('next').innerHTML=j.enabled
      ? `Next morning at <b>${j.next.morning.at}</b>, `
        +`next evening at <b>${j.next.night.at}</b>.`
      : 'Automatic mode is off — nothing will run on its own.';
  }catch(e){}
}
const demo=w=>post('/api/run',{which:w,speed:+$('speed').value,
  sim:$('sim').checked,ramp_minutes:+$('ramp').value,hold_s:+$('hold').value*60});

for(const k of ['motivational','piano']){
  const d=$('d-'+k);
  d.onclick=()=>{const i=document.createElement('input');
    i.type='file';i.multiple=true;i.accept='audio/*';
    i.onchange=()=>upload(k,i.files);i.click();};
  d.ondragover=e=>{e.preventDefault();d.classList.add('over');};
  d.ondragleave=()=>d.classList.remove('over');
  d.ondrop=e=>{e.preventDefault();d.classList.remove('over');upload(k,e.dataTransfer.files);};
}
async function upload(k,files){
  if(!files.length)return;
  const fd=new FormData(); for(const f of files) fd.append('files',f);
  toast(`Uploading ${files.length} file(s)…`);
  const r=await fetch('/api/library/'+k,{method:'POST',body:fd});
  const j=await r.json();
  toast(j.ok?`Added ${j.saved.length} file(s)`:'Upload failed',!j.ok); load();
}

document.addEventListener('keydown',e=>{
  if(e.code==='Space'&&$('run').classList.contains('on')
     &&!['INPUT','TEXTAREA'].includes(e.target.tagName)){
    e.preventDefault(); if(!$('rbtn').disabled) post('/api/click');}
});
window.addEventListener('beforeunload',e=>{if(DIRTY){e.preventDefault();e.returnValue='';}});

async function poll(){
  try{
    const s=await (await fetch('/api/status')).json();
    const on=s.active&&s.flow;
    $('run').classList.toggle('on',!!on);
    if(on){
      const f=s.flow;
      $('rphase').textContent=f.phase;
      $('rtext').textContent=f.task_text
        || {sunrise:'Sunrise',wake:'Good morning',get_up:'Time to get up',
            hold:'Goodnight',done_prompt:'All tasks complete',
            'winding down':'Winding down'}[f.phase] || '';
      $('rbtn').disabled=!f.waiting;
      $('rbtn').textContent=f.button||'—';
      const bits=[];
      if(f.tasks_total&&f.task_index>=0)bits.push(`task ${f.task_index+1} of ${f.tasks_total}`);
      if(f.light_pct!=null)bits.push(`light ${f.light_pct}%`);
      if(f.music)bits.push(`♪ ${f.music}`);
      if(f.nags)bits.push(`${f.nags} reminders`);
      $('rmeta').textContent=bits.join(' · ');
      $('runlog').textContent=(f.log||[]).join('\n');
    }
  }catch(e){}
}
load(); setInterval(poll,500); setInterval(pollNext,20000);
</script></body></html>"""




@app.get("/now", response_class=HTMLResponse)
async def now_page() -> str:
    return NOW


NOW = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>aura</title>
<style>
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:#000;color:#fff;
  font:16px/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;display:flex;align-items:center;
  justify-content:center;text-align:center;padding:6vh 6vw;overflow:hidden;
  cursor:default}
.stage{max-width:22em}
#clock{font-size:13px;letter-spacing:.28em;color:#555;margin-bottom:8vh;
  font-variant-numeric:tabular-nums}
#text{font-size:clamp(30px,7vw,64px);font-weight:200;letter-spacing:-.02em;
  line-height:1.2;min-height:1.2em;transition:opacity .35s}
#text.fade{opacity:0}
#sub{margin-top:3vh;font-size:14px;color:#666;min-height:1.4em}
#btn{margin-top:9vh;background:none;color:#fff;border:1px solid #444;
  border-radius:999px;padding:20px 56px;font-size:17px;letter-spacing:.14em;
  font-family:inherit;cursor:pointer;text-transform:uppercase;
  transition:background .18s,border-color .18s,opacity .3s}
#btn:hover:not(:disabled){background:#fff;color:#000;border-color:#fff}
#btn:disabled{opacity:0;pointer-events:none}
#hint{margin-top:3vh;font-size:12px;color:#3a3a3a;letter-spacing:.1em}
#idle{color:#444;font-size:18px;font-weight:200}
</style></head><body>
<div class="stage">
  <div id="clock"></div>
  <div id="text"><span id="idle">aura</span></div>
  <div id="sub"></div>
  <button id="btn" disabled>—</button>
  <div id="hint"></div>
</div>
<script>
const $=id=>document.getElementById(id);
let last='';
function setText(t){
  if(t===last)return; last=t;
  const el=$('text'); el.classList.add('fade');
  setTimeout(()=>{el.textContent=t; el.classList.remove('fade');},180);
}
function tick(){
  const d=new Date();
  $('clock').textContent=d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
}
setInterval(tick,1000); tick();

const PHASE={
  sunrise:'', wake:'Good morning', get_up:'Time to get up',
  done_prompt:'All tasks complete', hold:'Goodnight',
  'winding down':'', finished:'', idle:''
};
$('btn').onclick=()=>fetch('/api/click',{method:'POST'});
document.addEventListener('keydown',e=>{
  if((e.code==='Space'||e.code==='Enter')&&!$('btn').disabled){
    e.preventDefault(); fetch('/api/click',{method:'POST'});}
});

async function poll(){
  try{
    const s=await (await fetch('/api/status')).json();
    const f=s.flow, on=s.active&&f;
    if(!on){
      const nx=s.next||{};
      setText(' ');
      $('text').innerHTML='<span id="idle">aura</span>';
      $('sub').textContent=nx.morning
        ? `next: morning ${nx.morning.at} · evening ${nx.night.at}` : '';
      $('btn').disabled=true; $('hint').textContent=''; return;
    }
    setText(f.task_text || PHASE[f.phase] || '');
    const bits=[];
    if(f.tasks_total&&f.task_index>=0)bits.push(`${f.task_index+1} of ${f.tasks_total}`);
    if(f.phase==='sunrise')bits.push('sunrise');
    if(f.nags>2)bits.push(`${f.nags} reminders`);
    $('sub').textContent=bits.join('   ·   ');
    $('btn').disabled=!f.waiting;
    $('btn').textContent=f.button||'—';
    $('hint').textContent=f.waiting?'press space':'';
  }catch(e){}
}
poll(); setInterval(poll,400);
</script></body></html>"""


def discover() -> int:
    """Find WiZ bulbs on the LAN and offer to write the first one into aura.toml."""
    import re
    import socket

    async def go():
        from pywizlight import discovery

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            local = sock.getsockname()[0]
        except OSError:
            local = "192.168.1.1"
        finally:
            sock.close()
        bcast = ".".join(local.split(".")[:3]) + ".255"
        print(f"this machine is {local}, broadcasting to {bcast} ...\n")
        return await discovery.discover_lights(broadcast_space=bcast)

    try:
        bulbs = asyncio.run(go())
    except Exception as e:  # noqa: BLE001
        print(f"discovery failed: {e}")
        return 1

    if not bulbs:
        print("no bulbs found.")
        print("  - is 'Allow local communication' enabled in the WiZ app?")
        print("  - WiZ is 2.4GHz only; can this machine reach that network?")
        print("  - some routers block broadcast between wired and wireless clients")
        return 1

    for b in bulbs:
        print(f"  found {b.ip}  (mac {b.mac})")

    path = HERE / "aura.toml"
    if not path.exists() and (HERE / "aura.toml.example").exists():
        shutil.copyfile(HERE / "aura.toml.example", path)
    text = path.read_text("utf-8")
    new = re.sub(r'(?m)^ip = ".*"$', f'ip = "{bulbs[0].ip}"', text, count=1)
    if new != text:
        path.write_text(new, "utf-8")
        print(f"\nwrote {bulbs[0].ip} into aura.toml")
    else:
        print(f"\ncould not update aura.toml automatically — set ip = \"{bulbs[0].ip}\"")
    print("set a DHCP reservation for that address in your router.")
    return 0


def audio_check() -> int:
    """
    Open the audio device on its own, printing each step.

    If the process dies here, the last line printed names the driver that
    killed it — SDL failures are C-level and leave no Python traceback.
    """
    print("aura audio check\n")
    try:
        import pygame
        print(f"pygame-ce {pygame.version.ver}, SDL {'.'.join(map(str, pygame.get_sdl_version()))}")
    except Exception as e:  # noqa: BLE001
        print(f"FAILED to import pygame: {e}")
        return 1
    try:
        Player().start(log=print)
    except Exception as e:  # noqa: BLE001
        print(f"\nFAILED: {e}")
        return 1
    print("\naudio device opened. Playing a 2-second test tone ...")
    try:
        from aura.audio import make_keepalive_wav
        import array, math, wave
        tone = HERE / "audio" / "_test.wav"
        tone.parent.mkdir(parents=True, exist_ok=True)
        n, rate = 2 * 44100, 44100
        data = array.array("h", (int(9000 * math.sin(2 * math.pi * 440 * i / rate))
                                 for i in range(n)))
        with wave.open(str(tone), "w") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
            w.writeframes(data.tobytes())
        pl = Player(); pl.start(log=print); pl.play(tone, 0.5, block=True)
        tone.unlink(missing_ok=True)
        print("done — if you heard a beep, audio is working.")
    except Exception as e:  # noqa: BLE001
        print(f"playback failed: {e}")
        return 1
    return 0


if __name__ == "__main__":
    import uvicorn

    if "--audio-check" in sys.argv:
        raise SystemExit(audio_check())
    if "--discover" in sys.argv:
        raise SystemExit(discover())

    for d in (TTS_DIR, MOTIVATION_DIR, PIANO_DIR, CUSTOM_DIR, NAG_DIR):
        d.mkdir(parents=True, exist_ok=True)
    print(f"aura        http://127.0.0.1:{PORT}       settings")
    print(f"            {NOW_URL}   control screen")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
