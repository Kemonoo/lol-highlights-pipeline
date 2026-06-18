# Background music

Music files live here **locally only** — they're gitignored (`assets/music/*.mp3`) to
avoid committing binaries / redistributing tracks. A fresh clone has no audio until you
drop the tracks back in. The credits stage auto-appends `video.music_attribution` (set
per run from the chosen track) to every video description.

## What the pipeline expects

`pipeline/config.yaml` → `video.music_tracks` lists the tracks (one is picked at random
per run, with its attribution). Current set (royalty-free **NCS — NoCopyrightSounds**,
free on monetized YouTube *with* the per-track attribution the credits stage adds):

| file | artist | title |
|------|--------|-------|
| `ncs-why-we-lose.mp3`  | Cartoon       | Why We Lose (feat. Coleman Trapp) |
| `ncs-fearless-ii.mp3`  | Lost Sky      | Fearless pt. II (feat. Chris Linton) |
| `ncs-on-and-on.mp3`    | Cartoon       | On & On (feat. Daniel Levi) |
| `ncs-blank.mp3`        | Disfigure     | Blank |
| `ncs-invincible.mp3`   | DEAF KEV      | Invincible |
| `ncs-superhero.mp3`    | Unknown Brain | Superhero (feat. Chris Linton) |

Re-download from **ncs.io** (search the title) and save with the filename above.

## In-house fallback bed

`bg.mp3` is a numpy-synthesized, copyright-free bed. Rebuild it any time with:

```
python -m pipeline.tools.gen_music
```

Set `video.music_path: assets/music/bg.mp3` to force it instead of the NCS list.

## Other royalty-free sources (if you swap tracks)

- **Pixabay Music** (pixabay.com/music) — free, no attribution required, monetization OK.
- **YouTube Audio Library** (studio.youtube.com → Audio Library, filter "Attribution not
  required") — safest for Content ID.
- **Kevin MacLeod** (incompetech.com) — CC-BY 4.0, free with attribution.
