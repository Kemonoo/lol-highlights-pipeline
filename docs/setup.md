# Setup

The two steps that actually take time are registering a Twitch application and setting
up YouTube OAuth. Everything else is `pip install` and a config toggle.

Run `python -m pipeline.doctor` at any point — it tells you exactly what is still
missing and prints the command to fix it.

---

## 1. Python and FFmpeg

```bash
python -m venv venv
venv\Scripts\pip install -e .            # Windows
# python3 -m venv venv && venv/bin/pip install -e .     # macOS / Linux
```

Optional extras, installed only if you want them:

```bash
pip install -e ".[transcribe]"   # English captions on foreign-language clips
pip install -e ".[tts]"          # neural voiceover (produced mode)
pip install -e ".[upload]"       # YouTube publishing
pip install -e ".[anthropic]"    # Claude as an LLM provider
pip install -e ".[all]"          # everything above
pip install -e ".[dev]"          # pytest + ruff
```

**FFmpeg** must be on your PATH — it does all the rendering and cannot be installed
with pip:

| | |
|---|---|
| Windows | `winget install Gyan.FFmpeg` (reopen your terminal afterwards) |
| macOS | `brew install ffmpeg` |
| Debian/Ubuntu | `sudo apt install ffmpeg` |

Verify with `ffmpeg -version` and `ffprobe -version`.

---

## 2. Twitch API keys (required)

This is where the clips come from, so the pipeline can't run without it. It's free.

1. Go to the [Twitch developer console](https://dev.twitch.tv/console/apps) and log in.
   You'll need 2FA enabled on your Twitch account before it lets you register anything.
2. **Register Your Application**.
   - **Name** — anything unique across all of Twitch. If it's rejected, it's taken;
     add a suffix.
   - **OAuth Redirect URLs** — `http://localhost`. The form demands one, but this
     pipeline uses client-credentials auth and never redirects anywhere.
   - **Category** — *Application Integration*.
   - **Client Type** — *Confidential*.
3. Create it, then open it and copy the **Client ID**.
4. **New Secret** → copy it immediately. Twitch shows it once.
5. Put both in `.env`:

```ini
TWITCH_CLIENT_ID=your_client_id
TWITCH_CLIENT_SECRET=your_client_secret
```

`python -m pipeline.doctor` will confirm they're picked up. Note it only checks the keys
are *present* — a typo surfaces as a 401 on the first fetch.

---

## 3. Ollama (recommended — free local filtering)

The local vision filter is what discards ~90% of clips before anything paid runs. Skip
it and the `vlm_filter` stage cannot run.

1. Install from [ollama.com](https://ollama.com).
2. Pull a vision model:

```bash
ollama pull qwen3-vl:4b     # ~3GB, fits 4GB VRAM - a good default
ollama pull qwen3-vl:8b     # better, needs ~8GB
```

3. Leave Ollama running. `model: auto` in `llm.roles.vlm` picks the best one installed.

Running Ollama on another machine? Point `base_url` at it:

```yaml
llm:
  roles:
    vlm:
      base_url: http://192.168.1.50:11434
```

---

## 4. Gemini API key (optional, ~1¢/day)

Without it the pipeline still produces a video — clips are scored from local signals
instead. With it, selection gets substantially better, because an LLM actually watches
each clip with its audio.

1. Get a key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
2. Add it to `.env`:

```ini
GEMINI_API_KEY=your_key
```

**For AI thumbnails only**, you also need pay-as-you-go billing enabled on the API
project — image models are not on the free tier, and a free-tier key returns a quota of
zero rather than an error you'd recognise. The default `thumbnail.provider: local` uses
the free PIL design and needs no billing; switch to `gemini` once billing is on.

> **Model IDs get retired.** Google shuts models down on a schedule — `gemini-2.0-flash`
> went away mid-2026 and the 2.5 family follows in Oct 2026. Every model ID in this repo
> lives in one place, `llm.roles.*` in `config.yaml`. If a stage starts failing with
> HTTP 404, check [Google's deprecation page](https://ai.google.dev/gemini-api/docs/deprecations)
> and change the ID there. `python -m pipeline.doctor` detects this and says so.

---

## 5. YouTube upload (optional)

Only needed if you want the pipeline to publish. Rendering works without it.

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create a
   project (or pick one).
2. **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **APIs & Services → OAuth consent screen**:
   - User type **External**.
   - Fill in app name, your email, developer contact. Nothing else is required.
   - **Scopes** — you can skip adding them here; the app requests them at runtime.
   - **Test users** — *add your own Google account*. This is the step everyone misses:
     while the app is in "Testing", only listed test users can authorise it, and the
     flow fails with `access_blocked` otherwise.
   - Leave it in **Testing**. You do not need Google verification for personal use.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type **Desktop app**.
   - Create, then **Download JSON**.
5. Save that file as **`client_secret.json`** in the repo root. It's gitignored.
6. Enable uploading in your config:

```yaml
upload:
  enabled: true
  privacy: private      # keep this until you've watched a few finished videos
```

The first upload opens a browser once to authorise. Google will warn that the app isn't
verified — that's expected for a personal desktop client; continue via **Advanced**. The
token is cached at `data/yt_token.json`.

**Quota:** the YouTube Data API gives 10,000 units/day and an upload costs ~1,600, so
about 6 uploads a day. One long-form plus three Shorts fits comfortably.

**Custom thumbnails** require a phone-verified YouTube channel. Without it the video
uploads fine and just keeps an auto-generated thumbnail.

---

## 6. Music (optional)

Tracks aren't distributed with the repo (size + redistribution terms). See
`assets/music/README.md` to source them, or generate a royalty-free bed:

```bash
python -m pipeline.tools.gen_music
```

Music is off by default in lean mode (`video.music_enabled: false`); the outro still
uses a track when one is available.

---

## 7. Running it daily

> **Read this section before running the wrappers.** They are allowed to wake your PC
> and — if you turn it on — put it back to sleep. Nothing here happens until you set it
> up, but the defaults are worth knowing.

### Windows

```bat
copy auto_run.local.cmd.example auto_run.local.cmd
REM edit it, then:
setup_schedule.bat            REM registers the job for 03:00
setup_schedule.bat 05:30      REM ...or any time you prefer (24h HH:MM)
```

`setup_schedule.bat` prints a summary of what it just registered — the time, which
config overlay the run will use, and whether it will sleep the PC afterwards — so you
can check it against what you actually wanted.

**What the scheduled job does, and the defaults:**

| Behaviour | Default | Where to change it |
|---|---|---|
| Time of day | **03:00** | argument to `setup_schedule.bat` |
| Wake the PC if asleep | **yes** (`WakeToRun`) | Task Scheduler, or delete the task |
| Stay awake for the whole run | always | — (see below) |
| Which config the run uses | **plain `config.yaml`** — renders, uploads nothing | `CONFIG` in `auto_run.local.cmd` |
| Sleep the PC when finished | **no** | `SLEEP_AFTER` in `auto_run.local.cmd` |
| Retry a failed run | once, after 10 min | `RETRY_SECONDS` |

Settings live in **`auto_run.local.cmd`**, not in the `.bat` itself. That file is
gitignored, so your machine's choices survive a `git pull` instead of turning into a
merge conflict — the same reasoning as the `config.local.*.yaml` overlays.

**Waking and staying awake.** A PC woken by a wake timer is in an *unattended* wake, and
Windows sends it back to sleep after about two minutes of that — long before a run
finishes. So the job runs through `scripts/keep_awake.ps1`, which holds
`ES_SYSTEM_REQUIRED` until the pipeline exits and releases it afterwards, including on
Ctrl-C. The display is deliberately not kept on. If the machine never wakes at all,
check that wake timers are permitted in the active power plan:

```bat
powercfg /q | findstr /i "wake"
```

**Sleeping afterwards.** With `SLEEP_AFTER=1`, `scripts/sleep_prompt.ps1` runs at the
end. It distinguishes two cases:

- **Nobody is there** — no interactive desktop, or the session has been idle for
  `SLEEP_IDLE_MINUTES` (default 5). It suspends immediately; at 03:00 there is nobody
  to ask.
- **You are at the PC** — a small always-on-top window counts down
  `SLEEP_PROMPT_SECONDS` (default 120). *Stay awake*, Esc, or closing the window all
  cancel it; only *Sleep now* or letting the timer run out suspends the machine.
  Cancelling is the default in every ambiguous case.

With hibernation enabled, Windows may hibernate rather than suspend — `powercfg
/hibernate off` if you want true sleep.

To remove the job entirely:

```bat
schtasks /delete /tn "LoL Daily Highlights" /f
```

### macOS / Linux

```cron
0 3 * * *  cd /path/to/lol-highlights-pipeline && ./run_daily.sh >> data/logs/cron.log 2>&1
```

Same settings, read from the environment or from an optional `auto_run.local.sh`:
`CONFIG`, `SLEEP_AFTER`, `SLEEP_PROMPT_SECONDS`, `RETRY_SECONDS`. The run is wrapped in
`systemd-inhibit` (Linux) or `caffeinate` (macOS) when either is available, for the same
reason as `keep_awake.ps1`. `SLEEP_AFTER=1` suspends via `systemctl suspend` /
`pmset sleepnow` after a terminal countdown that any keypress cancels.

Note that **cron does not wake a sleeping machine** — it only fires if the box is
already up. To have it wake itself, schedule that separately with `rtcwake` (Linux) or
`sudo pmset repeat wakeorpoweron MTWRFSU 02:55:00` (macOS).

Both wrappers retry once after 10 minutes if the run fails, which covers Ollama still
starting up, a network blip, or a rate limit. The per-stage caches mean the retry only
redoes what's missing.

---

## Watching a run

A cold run takes a while — the local vision filter is the slow part. From a second
terminal:

```bash
python -m pipeline.progress            # one-shot
python -m pipeline.progress --watch    # refresh until it finishes
```

Progress is derived from the per-clip caches the run already writes, so it works
regardless of how the run was started (cron, Task Scheduler, another terminal) and
flags a stage that has written nothing for ten minutes.

To see *why* clips were kept or dropped, open `data/work/<date>/report.html`.

If a run is competing with your own work, throttle it:

```yaml
resources:
  cpu_percent: 70       # share of cores for CPU-bound work
  low_priority: true    # below-normal scheduling; ffmpeg children inherit it
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Cannot reach Ollama` | Ollama isn't running. Start the app, or `ollama serve`. |
| HTTP 404 from a model | The model ID was retired — see the note in section 4. |
| `access_blocked` during YouTube auth | Your Google account isn't in the OAuth consent screen's test-user list. |
| Transcription crashes with a cuDNN error | Missing cuDNN 9: `pip install nvidia-cudnn-cu12`. `transcribe.device: auto` avoids this by testing before selecting CUDA. |
| Text missing from rendered video | No usable font found. Set `video.font` to a `.ttf` path. |
| Video is short / few clips kept | Normal on a quiet day. Check `data/work/<date>/report.html` to see what was rejected and why. |

Anything else: run `python -m pipeline.doctor` first — it catches most of this.
