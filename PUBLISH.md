# Publishing this to GitHub

Run these from inside this folder. One time only.

## If you have the GitHub CLI

```powershell
gh auth login          # skip if already signed in
git init
git add -A
git commit -m "aura: sunrise alarm and wind-down routine"
gh repo create aura-routine --public --source=. --remote=origin --push
```

Done — it'll print the URL.

## If you don't

1. Go to <https://github.com/new>
2. Name: `aura-routine`, Public, and **do not** tick "Add a README" — the repo must start empty.
3. Then:

```powershell
git init
git add -A
git commit -m "aura: sunrise alarm and wind-down routine"
git branch -M main
git remote add origin https://github.com/sebgle/aura-routine.git
git push -u origin main
```

## Check before you push

```powershell
git status --short
```

You should see about 19 files and **none** of these:

- `settings.json` — your times and tasks
- `aura.toml` — your bulb's address
- `audio/voice/`, `audio/custom/`, `audio/nags/` — recordings
- `audio/motivational/*`, `audio/piano/*` — your music
- `.venv/`

If any of those show up, `.gitignore` isn't being applied — stop and check you're in the right folder.

## Later, from the other computer

```powershell
git clone https://github.com/sebgle/aura-routine.git
cd aura-routine
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup.ps1
```
