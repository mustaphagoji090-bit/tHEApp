# AI Story Image Generator

Upload your script and your voiceover, get back a finished video with an image every few
seconds, cut to the narration.

Built for long-form AI story videos: a 40-minute voiceover at 10 images per minute becomes
400 timed images and a rendered MP4, without touching a timeline by hand.

---

## What it does

```
your script.txt  +  voiceover.mp3
        │
        ├─ 1. Transcribe the voiceover for word-level timings (skipped for .srt/.vtt)
        ├─ 2. Match YOUR script onto those timings — your wording wins, always
        ├─ 3. Cut the timeline into beats — 10 per minute, snapped to sentence ends
        ├─ 4. Write a style bible + locked character descriptions (OpenAI or Claude)
        ├─ 5. Write one image prompt per beat, all obeying that style bible
        ├─ 6. Generate every image in parallel (fal.ai / Replicate / OpenAI)
        └─ 7. Render: Ken Burns motion, crossfades, voiceover, optional music bed
        │
    final.mp4  +  numbered stills  +  timeline.csv
```

**Nothing here writes your script.** You supply it. The text model writes the ~400 *image
prompts* — the text handed to the image model for each beat — which is a different job and
not something anyone hand-writes 400 times.

The two things that make the output usable rather than a slideshow of strangers:

- **Your script is the source of truth, the audio only supplies timing.** Whisper hears
  "Mira" where your script says "Mara"; the transcript is matched against your script and
  discarded, so the misheard spelling never reaches an image prompt. Beat cuts are anchored
  to a fixed grid, so the average rate stays exactly the images-per-minute you asked for even
  when individual cuts move to land on a sentence end.
- **Characters stay the same person.** Image models have no memory between calls, so a
  style bible is written once per video with a locked physical description for each recurring
  character, and every prompt that features them repeats that description verbatim.

---

## Quickstart

This runs on your own machine — there is no hosted site, so your API keys and your scripts
never leave your computer. You need Python 3.10+; ffmpeg is bundled, so there is nothing else
to install.

**macOS / Linux**

```bash
git clone -b claude/ai-stories-image-gen-rd8a1p https://github.com/mustaphagoji090-bit/tHEApp
cd tHEApp
./run.sh
```

**Windows** — same first two lines, then `run.bat` instead of `./run.sh`.

The first run builds a virtualenv, then stops and tells you to fill in `.env`. Open that file
and paste in one line:

```ini
OPENAI_API_KEY=sk-...
```

That single key covers everything — transcription, image prompts and images. Run `./run.sh`
(or `run.bat`) again and open <http://127.0.0.1:8000>.

> **Test on a short clip first.** Point it at a 1–2 minute voiceover before you feed it a
> 40-minute one. You see the whole pipeline end to end for roughly 20 cents, and you can judge
> the look before committing to 400 images.

Later, if you want cheaper and faster images than OpenAI's, add `FAL_KEY=...` to `.env` and
pick **fal** in the image-provider dropdown.

### Who does what

| Job | Who | Swap with |
|---|---|---|
| Your script | **You** | — |
| Word-level timing | Whisper (`OPENAI_API_KEY`) | Supply an `.srt`/`.vtt` and this is skipped |
| Writing the ~400 image prompts | OpenAI `gpt-4o` by default | `DIRECTOR_PROVIDER=anthropic`, or `OPENAI_TEXT_MODEL=gpt-4o-mini` |
| Generating images | Your pick | `fal` / `replicate` / `openai`, per job in the UI |

Prompt writing costs roughly **80k–150k tokens per 40-minute video** — on `gpt-4o` that is a
few dollars; on `gpt-4o-mini`, cents.

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

1. Paste or drop in your script, and drop in the voiceover. Both are required.
   If your script is an `.srt` or `.vtt`, its own timings are used and transcription is
   skipped — faster and free.
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
| Prompt writer | OpenAI | Switch to Claude per job if you prefer its prompts. |
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
`ALL CHECKS PASSED (12/12)` and leave a rendered test video behind. It also checks that
script-only spellings survive alignment into the finished beats.

---

## Layout

```
backend/
  main.py        FastAPI routes
  jobs.py        job store + pipeline orchestration (background thread per job)
  transcribe.py  Whisper, with chunking for long files
  beats.py       timeline slicing — grid-anchored, sentence-snapping
  script_align.py  binds your script to the audio's timings; parses .srt/.vtt
  director.py    style bible, character sheet, per-beat prompts
  writers.py     swappable text backends (OpenAI / Anthropic)
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
the `PROVIDERS` dict. It shows up in the UI automatically once its key is set. The same
pattern applies to text models — subclass `Writer` in `backend/writers.py`.

---

## Notes and limits

- **Whisper is required for timing unless you upload subtitles.** Without `OPENAI_API_KEY`
  there are no word timestamps — but an `.srt`/`.vtt` script carries its own, so that path
  needs no transcription at all. A local `faster-whisper` option would remove the dependency
  entirely — not built yet.
- **The "Script sync" stat tells you if the script and audio actually match.** A high
  percentage means alignment worked. If it reads "no match", the script and voiceover are
  different content and the app falls back to the transcript.
- **Rendering 400 clips is CPU-heavy.** Segments render in parallel across your cores, then
  merge as a tree (groups of 10) so ffmpeg never sees a 400-input filtergraph.
- **Prompts are written for advertiser-safe images** — violence is implied through aftermath
  and reaction rather than depicted. Adjust the rules in `director.py` if your niche differs.
- Long uploads are streamed to disk, so a 40-minute MP3 will not blow up memory.
