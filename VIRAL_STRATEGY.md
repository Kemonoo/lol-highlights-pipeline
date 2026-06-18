# Viral Strategy — research → ranked roadmap

Goal: turn the automated LoL daily-highlights channel into something that actually
*grows*. This is the prioritized plan; we work it top-down, one item at a time.

## The only two things the algorithm rewards

Every credible source (MrBeast's leaked 36-page production guide, his ViewStats
platform, and current thumbnail/retention guides) collapses to two metrics:

1. **CTR — Click-Through Rate.** Driven by the *packaged combination* of **idea +
   thumbnail + title**. MrBeast's team builds the title/thumbnail *first* and makes
   ~50 thumbnail variants per video; ViewStats exists purely to A/B-test packaging.
   "Package the combination correctly and you incentivize a click; package it wrong
   and they keep scrolling."
2. **AVD / retention — Average View Duration.** "The first minute is where retention
   is won or lost." The steepest drop is ~second 10–20, where the viewer decides if
   the video delivers the thumbnail's promise. Cut intros to <5s; lead with the
   payoff; escalate; never let a boring moment sit.

Two implications for us specifically:
- **The thumbnail/title must not lie** — a clickbait promise the video doesn't deliver
  spikes CTR but craters retention, which the algorithm punishes harder. So packaging
  and our clip-quality filtering have to move together.
- **We can automate A/B testing** — YouTube's "Test & Compare" now lets you upload
  3 thumbnails and auto-pick the winner by watch-time. That's a perfect fit for an
  automated pipeline that can generate variants for free.

## Ranked roadmap

| # | Item | Lever | Effort | Status |
|---|------|-------|--------|--------|
| 1 | **Packaging: titles + thumbnails (incl. A/B variants)** | CTR | M | titles ✅, thumbnails next |
| 2 | **Cold-open hook** — front-load the #1 moment, kill the slow intro | AVD | M | planned |
| 3 | **Only-the-best filtering** (deliver the promise) | CTR+AVD | — | ✅ mostly done (judge/lang/local-fallback) |
| 4 | **Engagement CTAs** — subscribe + "comment your favorite #" + open loop | engagement/AVD | S | description ✅, spoken/on-screen next |
| 5 | **Retention editing** — trim dead air, beat-synced cuts, speed-ramps, progress bar | AVD | M-L | planned |
| 6 | **End screen / session** — outro CTA + end-screen window + "watch yesterday's" | session time | S | planned |
| 7 | **Branding & SEO** — consistent packaging, keyword-rich description/tags/hashtags | discovery | S | partial |
| 8 | **Analytics feedback loop** — pull CTR/AVD/retention, feed back into selection + packaging | compounding | L | planned |

Effort: S ≈ <1 session, M ≈ 1–2, L ≈ multi-session.

---

## 1 — Packaging: titles + thumbnails  *(highest leverage)*

**Why:** no clicks = no views; this is 80% of the battle and the one thing MrBeast
optimizes hardest.

**Titles (done this pass — see credits.py):**
- Curiosity + emotion over description. `"INSANE PENTAKILL?! 😱 LoL Best Moments"`
  beats `"PENTAKILL | League of Legends clip highlights 2026-06-15"`.
- **Drop the date** from the title — it makes the video look stale and kills evergreen
  CTR (date stays in description/tags for SEO).
- Lead with the strongest specific moment (penta/1v5/steal), add an emotional kicker
  and curiosity, keep ≤ ~70 chars, rotate phrasing so the channel doesn't look botted.

**Thumbnails (next):** current generator is a good base (champion splash + text). Apply
2026 best practices:
- **One emotional human face** (faces = +20–35% CTR). Use a *reaction/expression* crop,
  not just the small avatar — ideally the streamer's facecam at the peak moment, or a
  shocked expression. One subject, one message, readable in <1s on mobile.
- **≤3–4 words**, huge bold sans-serif, heavy stroke/glow (we have this).
- **High contrast + color pop** (we have the cinematic grade + vignette).
- **Curiosity object**: the champion mid-ability, a big number, a red arrow/circle on
  the kill.
- **Generate 3 variants** (different moment / text / face) and upload them via YouTube's
  Test & Compare API so the platform picks the winner automatically.

Files: `production/credits.py` (title), `production/thumbnail.py` (variants),
`publishing/upload.py` (A/B upload).

## 2 — Cold-open hook *(retention)*

**Why:** the slow "DAILY LEAGUE HIGHLIGHTS" + "welcome back" intro is exactly the
drop-off trap — viewers bail before content. The first 5–15s must *show*, not brand.

**How:** restructure `assemble.py`:
- Open with a 3–5s **montage of the single best moment** (the #1 clip's `best_moment_s`
  peak, hard-cut, loud) *before* any card — pattern interrupt.
- Overlay an **open loop**: "wait for #1…" to promise a payoff.
- Then a <3s branded sting, then the countdown. Keep total pre-content under ~8s.

## 3 — Only-the-best filtering *(deliver the promise)*

Mostly shipped this week: stronger judge prompt (drops walking/singing/no-combat),
local-signal fallback when Gemini is down, `ja/zh` language exclusion, English-only VO.
Ongoing: keep tuning so the thumbnail/title promise is always met.

## 4 — Engagement CTAs *(engagement + retention loop)*

- **Description (done):** sharpened "comment your favorite #" + subscribe ask.
- **Spoken (next):** intro VO line nudging subscribe + "comment which was #1"; a mid-roll
  beat. Keep it short — over-asking hurts.
- **On-screen (next):** subtle animated "SUBSCRIBE" lower-third once, and a "which was
  your favorite? 👇" card before the #1 reveal (doubles as the open-loop payoff).

## 5 — Retention editing *(AVD)*

- Trim dead frames at clip head/tail; tighten the per-clip window to the action.
- **Beat-synced cuts** to the music bed; **speed-ramp** into the slow-mo replay.
- A thin **countdown progress element** ("3 of 18") to create a finish-line pull.
- Punchier transitions between segments (whip/zoom) instead of plain fades.

## 6 — End screen / session time

- Outro: explicit "subscribe + watch yesterday's top plays" with a 20s end-screen
  window (black-safe area) so YouTube end-screen elements fit.
- Set the uploaded video's end screen to link the previous day's video (playlist/session).

## 7 — Branding & SEO

- Consistent packaging: the animated PROJECT nameplate (done), fixed color identity,
  recurring title pattern, channel-watermark.
- Keyword-rich description + 1–3 hashtags (#leagueoflegends #lolhighlights), streamer
  names as tags (partly done).

## 8 — Analytics feedback loop *(compounding)*

Extend the existing `feedback/feedback.py` stage to pull **YouTube Analytics** (CTR,
AVD, retention curve, which thumbnail won the A/B). Feed it back: prefer clip *types*
that historically retained, and learn which title/thumbnail styles win. This is what
turns the channel into a self-improving system — but it needs upload history first.

---

## Sources
- [How to succeed in MrBeast production — leaked PDF summary (Simon Willison)](https://simonwillison.net/2024/Sep/15/how-to-succeed-in-mrbeast-production/)
- [Leaked MrBeast YouTube strategies (CTR/AVD/AVP)](https://protunesone.com/blog/leaked-mrbeast-document-on-his-youtube-strategies/)
- [ViewStats — MrBeast's packaging/A-B analytics platform](https://www.viewstats.com/info)
- [YouTube "Test & Compare" titles + thumbnails (Tubefilter)](https://www.tubefilter.com/2025/07/16/youtube-feature-test-and-compare-titles-thumbnails/)
- [YouTube thumbnail best practices 2026 (faces, contrast, ≤4 words)](https://awisee.com/blog/youtube-thumbnail-best-practices/)
- [First-30-seconds hook framework / cut the intro](https://1of10.com/blog/how-to-hook-viewers-in-the-first-30-seconds-of-a-youtube-video/)
