# aura

A sunrise alarm and wind-down routine for one bedroom. It runs on a Windows PC
you leave on, drives a smart bulb and a wired speaker, and asks you to press a
button as you finish each thing.

Two routines, both automatic:

**Morning** — the light climbs from dark to full over 30 minutes, timed so it
reaches 100% exactly at your wake time. Then it says good morning, tells you to
get up, and reminds you every ten seconds until you press **I'M UP**. Your
motivational audio starts on that press. Each task is announced over the music,
which drops to silence for the announcement and comes back after. Press **DONE**
as you finish each one. At the end it says so, you press **FINISH**, and the
music fades.

**Evening** — the light dims and piano starts. Each task is announced, you press
**DONE**, and the light drops another notch, reaching 1% as the last task
completes. It says goodnight and holds at 1% — just enough to find the bed —
until your bedtime, when the music fades and the light goes out.

The wind-down start time is calculated backwards: **bedtime − your task
durations − a 15-minute buffer**. Add a task and everything shifts earlier by
itself.

---

## What you need

| | |
|---|---|
| **A PC** | Windows, left on and signed in. Python 3.11+. |
| **A bulb** | WiZ, tunable white or colour. Any lamp with a standard socket. |
| **A speaker** | **Wired** to the PC. See [Why wired](#why-wired). |

---

## Setup

```powershell
git clone https://github.com/sebgle/aura-routine.git
cd aura-routine
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup.ps1
```

`setup.ps1` creates the virtual environment, installs everything, checks your
audio device, and finds your bulb. It's safe to re-run.

**Before you run it:** open the WiZ app → your bulb → and turn on **Allow local
communication**. Nothing works without it, and it's the first thing to check if
the bulb can't be found.

Then:

```powershell
.\.venv\Scripts\python.exe aura_web.py
```

- **Settings** — <http://127.0.0.1:8770>
- **Control screen** — <http://127.0.0.1:8770/now>

Both bind to `127.0.0.1` only and aren't reachable from your network.

### First time through the settings page

1. Set your **wake** and **asleep-by** times. The schedule underneath recalculates as you type.
2. Edit the **morning** and **evening** tasks. Evening tasks have a duration — that's what decides when the wind-down starts.
3. Press **Prepare voice**. This synthesises anything you haven't recorded yourself.
4. Optionally press ● next to any task to **record it in your own voice** — that always overrides the synthesised version.
5. Record a few **get-up reminders**. They play in rotation, not at random.
6. Drop audio into the two libraries.
7. Press **Run morning** to try it. Set *Sunrise length* to 1 minute so you're not waiting.

### Making it automatic

```powershell
.\install_startup.ps1
Start-ScheduledTask -TaskName aura
```

Starts at logon, restarts if it crashes, no console window. Then three Windows
settings, none of them optional:

- **Sleep and hibernate off.** A sleeping PC runs nothing.
- **Windows Update active hours** covering your sleep window. A 03:00 reboot is
  the most likely way this silently stops working.
- **Stay signed in.** The task runs in your session.

Worth testing once: force a reboot at 22:00, walk away, and check it fires.

---

## Design notes

The decisions that are load-bearing, and why — so future-you doesn't undo them.

### Why wired

The speaker is wired to the PC because Bluetooth can't be shared. Whichever
device is paired owns the speaker, so if your phone is connected, the system is
mute. Wired means both music and voice are just streams into the same mixer:
an announcement plays *over* the music with nothing to stop, restore, or get
wrong.

**Prefer the analogue input over USB.** On USB the soundbar *is* the audio
device, so if it powers down its sink vanishes and Windows silently reroutes
audio somewhere else — no error, no log, you just don't wake up. On analogue the
sink is the PC's own codec, which is always present. That principle shows up
repeatedly here: **prefer the component that can't disappear over the one that
sounds better.**

### No live text-to-speech at 7am

Every line is rendered to disk when you edit it. Playback is reading a file. A
TTS outage, an expired token or a dead connection cannot get between you and
waking up.

That's also why a *cloud* TTS is acceptable when a cloud *light* would not be —
the network dependency exists at render time and nowhere else.

Rendering is content-addressed: the cache key hashes the text, engine, voice and
rate, so editing one task re-renders one clip.

### The keep-alive tone

A 19 kHz tone at about −54 dBFS loops on a reserved channel the whole time it's
running. Inaudible, and it stops the soundbar's auto-standby from sleeping and
eating the first word of a 07:00 announcement. Padding every clip with silence
would have worked around the problem; this makes it stop existing.

### The perceptual curve

Brightness follows `t^2.4`, colour temperature `t^1.6`, quantised to 25 K.
Halfway through the ramp the light is only at 20% — the rise is deliberately
back-loaded, which is what makes it read as a sunrise instead of a dimmer being
turned up. Colour lags brightness so the light stays warm through the first half
rather than going cold while still dim. About a third of the ramp sits below
10%, which is why bulb choice matters more than it looks.

### Failure is expected, not exceptional

The most common real failure isn't a crash — it's the lamp switched off at the
socket, or the network not up yet after a reboot. Both look like silence, so
both are retried for five minutes and then reported with the actual likely
cause rather than a stack trace.

Audio device initialisation tries four configurations in order and logs each
attempt *before* making it, because SDL can take the whole process down at the
C level with no Python traceback — so the last line printed names the culprit.

Uploaded recordings are parsed with the `wave` module before being kept, and
auditioning plays in the browser rather than through the audio engine. A
malformed file should be a message, not a dead server.

### The scheduler

Checks every 20 seconds and writes the date of the last fire to disk, so
restarting the app at 06:45 doesn't start a second sunrise. If it fires late —
PC was asleep — the ramp is shortened so it still lands on your wake time.
Past a grace window it skips the day rather than running a sunrise at lunchtime.

---

## Commands

```powershell
python aura_web.py               # run it
python aura_web.py --discover    # find your bulb, write it to aura.toml
python aura_web.py --audio-check # open the audio device, print each attempt, beep
```

## Configuration

| File | What | In git? |
|---|---|---|
| `aura.toml` | Bulb IP, TTS voice | no — copied from `aura.toml.example` |
| `settings.json` | Times, tasks, volumes, fade lengths | no — the settings page writes it |
| `audio/voice/` | Synthesised speech | no — regenerated in seconds |
| `audio/custom/`, `audio/nags/` | Your recordings | no |
| `audio/motivational/`, `audio/piano/` | Your music | no |

Everything personal stays on your machine. A clone gives you the code and
nothing else.

A few things aren't exposed in the UI and live in `settings.json`:
`get_up_nag_interval_s` (10), `duck_volume` (0.0 — music fully silent under the
voice), `morning_fade_s` (3), `music_fade_s` (20, evening), and
`night_buffer_minutes` (15).

## Licence

MIT.
