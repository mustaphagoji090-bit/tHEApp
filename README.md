# AI Story Image Generator

Upload a voiceover (and your script), get back a finished video with an image every few
seconds, cut to the narration.

Built for long-form AI story videos: a 40-minute voiceover at 10 images per minute becomes
400 timed images and a rendered MP4, without touching a timeline by hand.

---

## What it does

```
voiceover.mp3  +  script/title
        │
        ├─ 1. Transcribe the voiceover (Whisper, word-level timestamps)
        ├─ 2. Cut the timeline into beats — 10 per minute, snapped to sentence ends
        ├─ 3. Write a style bible + locked character descriptions (Claude)
        ├─ 4. Write one image prompt per beat, all obeying that style bible
        ├─ 5. Generate every image in parallel (fal.ai / Replicate / OpenAI)
        └─ 6. Render: Ken Burns motion, crossfades, voiceover, optional music bed
        │
    final.mp4  +  numbered stills  +  timeline.csv
```

The two things that make the output usable rather than a slideshow of strangers:

- **Timing comes from the audio, not from guesswork.** Whisper gives word-level timestamps,
  so every image has an exact in/out point and cuts land on sentence boundaries instead of
  mid-clause. Beat cuts are anchored to a fixed grid, so the average rate stays exactly the
  images-per-minute you asked for even when individual cuts move to find a sentence end.
- **Characters stay the same person.** Image models have no memory between calls, so a
  style bible is written once per video with a locked physical description for each recurring
  character, and every prompt that features them repeats that description verbatim.

---

## Setup

You need Python 3.10+. ffmpeg is used for rendering — if you do not have it installed, the
app falls back to the copy bundled with the `imageio-ffmpeg` package automatically, so there
is usually nothing to do.

```bash
git clone <this repo>
cd tHEApp
./run.sh          # creates a virtualenv, installs deps, writes .env
```

The first run stops and asks you to fill in `.env`:

```ini
ANTHROPIC_API_KEY=sk-ant-...   # required — writes the image prompts
OPENAI_API_KEY=sk-...          # required — Whisper transcription (this is what gives timings)
FAL_KEY=...                    # pick ONE image provider
```

Then run `./run.sh` again and open <http://127.0.0.1:8000>.

### Which image provider

| Provider | Set | ~Cost / image | 400 images | Notes |
|---|---|---|---|---|
| **fal.ai** | `FAL_KEY` | ~$0.025 | ~$10 | Recommended. Fastest, highest concurrency. |
| Replicate | `REPLICATE_API_TOKEN` | ~$0.035 | ~$14 | Same Flux models, slower cold starts. |
| OpenAI | `OPENAI_API_KEY` | ~$0.04+ | ~$16+ | Best prompt adherence, slowest. |

Costs are rough — check current provider pricing. Swap the exact model with `FAL_MODEL` /
`REPLICATE_MODEL` / `OPENAI_IMAGE_MODEL` in `.env`.

Prompt writing (Claude) adds roughly **$1–3 per 40-minute video**. The system prompt is cached
across batches so you are not billed for the style bible on every call.

---

## Using it

1. Drop in the voiceover. Paste the script if you have it — it fixes names and spellings the
   transcript would otherwise mangle.
2. Set images per minute (default 10), aspect ratio and a visual style.
3. Hit **Start**. Watch the stages tick over.
4. When the images land, click any shot to see its prompt, edit it, and regenerate just that
   one. Failed images can be retried in a batch.
5. The video renders automatically (turn that off if you would rather review first).

You also get, for every job:

- `final.mp4` — the rendered video
- **Images + timing sheet (.zip)** — stills named `0042_00-04-12-000.jpg`, so they drop into
  CapCut/Premiere in order with their timecodes
- `timeline.csv` — index, in/out, timecode, shot type, narration and the prompt for each image

So even if you want to do the edit by hand, the shot list and stills are ready.

---

## Settings worth knowing

| Setting | Default | Notes |
|---|---|---|
| Images per minute | 10 | 6–8 feels calmer, 12–15 is fast-cut. Drives cost directly. |
| Transition | Crossfade | **Hard cut** renders ~2.5× faster with no quality loss from re-encoding. |
| Aspect | 16:9 | `9:16` for Shorts, `1:1` for square. |
| Auto-render | on | Off gives you a review step before rendering. |
| Music bed | — | Loops to fit, ducked to −22 dB under the voice, limiter on the mix. |

Environment tuning in `.env`: `IMAGE_CONCURRENCY` (default 6 — raise it if your provider
allows), `DIRECTOR_MODEL`, `STORAGE_DIR`.

---

## How long does it take

Measured on a 60-second test job in a container (4 cores), scaled up:

| Stage | 40-minute video |
|---|---|
| Transcription | ~1–2 min |
| Prompt writing | ~2–4 min (batched 16 beats/call, 4 in parallel) |
| Image generation | ~15–40 min (400 images, 6 at a time — provider-bound) |
| Render, crossfade | ~25–45 min (CPU-bound, scales with cores) |
| Render, hard cut | ~10–18 min |

Rendering is the part that rewards a faster machine. Hard cuts skip re-encoding entirely
(stream-copy concat), which is why they are so much quicker.

---

## Checking your install

```bash
python tests/smoke_test.py
```

Runs the real beat slicing, job orchestration and ffmpeg render against fake transcription,
a fake director and a fake image provider. No API keys, no cost. It should print
`ALL CHECKS PASSED (10/10)` and leave a rendered test video behind.

---

## Layout

```
backend/
  main.py        FastAPI routes
  jobs.py        job store + pipeline orchestration (background thread per job)
  transcribe.py  Whisper, with chunking for long files
  beats.py       timeline slicing — grid-anchored, sentence-snapping
  director.py    Claude: style bible, character sheet, per-beat prompts
  render.py      ffmpeg: Ken Burns clips, crossfade merge tree, audio mux
  media.py       ffmpeg discovery, duration probing, audio prep
  imagegen/
    base.py      provider adapters (fal, replicate, openai)
    runner.py    bounded-concurrency batch generation with retries
frontend/        single-page UI (no build step)
tests/
  smoke_test.py  end-to-end check with mocked APIs
```

### Adding another image provider

Subclass `ImageProvider` in `backend/imagegen/base.py`, implement `generate()`, and add it to
the `PROVIDERS` dict. It shows up in the UI automatically once its key is set.

---

## Notes and limits

- **Whisper is required for timing.** Without `OPENAI_API_KEY` there are no word timestamps,
  and the whole point is timings that match the audio. A local `faster-whisper` path would
  remove that dependency — not built yet.
- **Rendering 400 clips is CPU-heavy.** Segments render in parallel across your cores, then
  merge as a tree (groups of 10) so ffmpeg never sees a 400-input filtergraph.
- **Prompts are written for advertiser-safe images** — violence is implied through aftermath
  and reaction rather than depicted. Adjust the rules in `director.py` if your niche differs.
- Long uploads are streamed to disk, so a 40-minute MP3 will not blow up memory.
