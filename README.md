# vedit

A small, fast video editor for Linux that imports what you actually have.

Every Linux NLE has a disqualifying flaw. DaVinci Resolve's free Linux build
refuses H.264/AAC in MP4 — the most common format anyone has. Kdenlive gets
unstable under load, Shotcut's timeline is clumsy, Olive is perpetually alpha.

vedit does the 90% case well: **import anything, cut it, export it.**

## What it does

Three pages, mirroring how the work splits up.

- **Media** — drag and drop files or folders into the pool. Thumbnails, waveform
  peaks and preview proxies generate in the background.
- **Edit** — preview viewer with a scrubber, and a timeline with three video and
  three audio lanes. Razor, trim, move, ripple delete, clip speed, undo.
- **Render** — pick a preset and a destination, queue it, watch it go.

## Requirements

- Python 3.11+
- `ffmpeg` and `ffprobe` on `PATH`
- PySide6 and PyAV (installed below)

An NVIDIA GPU is used for proxy generation and optional NVENC export when
present, but nothing requires it.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Run

```bash
.venv/bin/python -m vedit
```

## Formats

Import accepts **anything your ffmpeg can demux** — there is no allowlist. That
covers H.264, HEVC, VP8/VP9, AV1, ProRes, DNxHD/DNxHR, MJPEG and MPEG-2 video;
AAC, MP3, Opus, FLAC, AC3/E-AC3 and PCM audio; in MP4, MOV, MKV, WebM, AVI, MXF
and bare `.wav`/`.mp3`.

Export is deliberately a short list of presets that are known to work:

| Preset | Video | Audio |
|---|---|---|
| H.264 · MP4 | libx264 | AAC |
| H.264 · MP4 (NVENC) | h264_nvenc | AAC |
| HEVC · MP4 | libx265 | AAC |
| HEVC · MP4 (NVENC) | hevc_nvenc | AAC |
| ProRes 422 · MOV | prores_ks | PCM |
| DNxHR HQ · MOV | dnxhd | PCM |
| VP9 · WebM | libvpx-vp9 | Opus |

NVENC presets only appear when your ffmpeg actually has the encoder.

Clips of different resolutions and frame rates can be cut together freely; each
segment is normalised before concatenation.

## Keyboard

| Key | Action |
|---|---|
| `Space` | Play / pause |
| `J` / `K` / `L` | Shuttle back / pause / shuttle forward |
| `←` / `→` | Step one frame (`Shift` for one second) |
| `Home` / `End` | Go to start / end |
| `X` | Cut / split at the playhead |
| `Delete` | Ripple delete (closes the gap) |
| `Backspace` | Delete, leaving a gap |
| `F` | Zoom timeline to fit |
| `+` / `-` | Zoom in / out |
| `Ctrl+Z` / `Ctrl+Shift+Z` | Undo / redo |
| `Ctrl+A` / `Esc` | Select all / deselect |
| `Ctrl+S` / `Ctrl+O` | Save / open project |
| `Shift+1/2/3` | Media / Edit / Render page |

Mouse: drag a clip body to move it along the timeline or up and down onto
another lane, drag its edges to trim, `Ctrl`+wheel to zoom,
wheel to scroll lanes, `Shift`+wheel to scroll along the timeline. Click a track
header to mute it, `Shift`-click to lock it.

**Right-click a clip** for cut, speed (¼× to 4×, or a custom multiplier),
link/unlink, enable/disable and delete. **Right-click a track header** to mute,
lock, add, clear or delete lanes.

## How it works

A few decisions worth knowing about if you read the code.

**Time is integer frames.** [`core/timebase.py`](vedit/core/timebase.py) holds an
exact `Fraction` frame rate; floats only appear at the ffmpeg boundary. A clip's
`src_in` is an offset into its source measured in *timeline* frames, so cutting a
25 fps source into a 30 fps timeline needs no rate conversion anywhere — the
source is addressed by time, which is the unit both ffmpeg's `trim` filter and
PyAV's `seek` already want.

**Proxies make Python fast enough.** On import, each source gets a 540p H.264
proxy with a 12-frame GOP. The short GOP is the important part: a seek lands near
its target without a long decode run-up, which is the difference between
scrubbing that feels live (measured at 8–33 ms) and scrubbing that feels broken.
Renders always read the originals.

**The clock is steered, not read.** Audio devices report position in coarse
jumps. Reading that directly made playback lurch two or three frames at a time
and discard everything in between — 12 fps reaching the screen on a 30 fps
timeline. [`player/clock.py`](vedit/player/clock.py) free-runs on monotonic time
and is disciplined *toward* the audio position, correcting small errors gradually
and snapping only on large ones. Every decoded frame now gets displayed.

**Undo is snapshot-based.** Timelines are small enough that copying the whole
track list per edit costs far less than the bugs you get from hand-written
`undo()` methods drifting out of step with their `do()`.

**The timeline model knows nothing about Qt.** That is why it can be tested
exhaustively without a running application.

## Tests

```bash
.venv/bin/python -m pytest            # everything
.venv/bin/python -m pytest -m "not slow"   # skip the real ffmpeg renders
```

The slow tests generate their own fixtures with `lavfi`, run a real export of a
mixed 30/25 fps timeline, and probe the result to assert the output is
frame-exact against what the timeline claims.

## Not there yet

Transitions, effects, colour correction, opacity/blending between video layers,
and audio level automation.

Multiple lanes work, but as pure occlusion: whatever sits on the highest video
lane at a given frame is what you see, and all audio lanes are summed. Clip speed
changes pitch with it, like a tape machine — there is no pitch correction.
