# vedit

A small, fast video editor for Linux that imports what you actually have.

Every Linux NLE has a disqualifying flaw. DaVinci Resolve's free Linux build
refuses H.264/AAC in MP4 — the most common format anyone has. Kdenlive gets
unstable under load, Shotcut's timeline is clumsy, Olive is perpetually alpha.

vedit does the 90% case well: **import anything, cut it, export it.**

## What it does

Four pages, mirroring how the work splits up.

- **Media** — drag and drop files or folders into the pool. Thumbnails, waveform
  peaks, filmstrips and preview proxies generate in the background.
- **Edit** — preview viewer with a scrubber, and a timeline with three video and
  three audio lanes. Video clips show a filmstrip of frames and audio clips show
  their waveform, with the clip name in a band along the foot and a chain mark on
  anything still linked to its A/V partner. Razor, trim, move, ripple delete,
  clip speed, undo.
- **Audio** — the same timeline with only its audio lanes, drawn twice as tall.
  Drag the line across a clip to set its level, drag the top corners inward for
  fades, or right-click to normalise. A mixer along the foot gives every lane a
  dB fader, mute and solo, and a peak meter with a held peak and a clip
  indicator, plus a master. Everything you hear here is what gets exported.
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
| `Shift+1/2/3/4` | Media / Edit / Audio / Render page |

Mouse: click the ruler or an empty part of a lane to move the playhead — during
playback too, which jumps there and keeps playing. Drag a clip body to move it
along the timeline or up and down onto another lane, drag its edges to trim,
`Ctrl`+wheel to zoom,
wheel to scroll lanes, `Shift`+wheel to scroll along the timeline. Click a track
header to mute it, `Shift`-click to lock it.

**Right-click a clip** for cut, speed (¼× to 4×, or a custom multiplier),
link/unlink, enable/disable and delete; audio clips also offer normalise, reset
gain and clear fades. **Right-click a track header** to mute, lock, solo, reset
the lane gain, or add, clear and delete lanes.

On the Audio page, drag a clip's volume line up and down to set its gain (hold
`Shift` to fine-adjust, and it detents at 0 dB), and drag the small handles at
its top corners inward to fade in and out. Faders respond as you drag them and
land on the undo stack once, when you let go. Double-click a fader for 0 dB.

## How it works

A few decisions worth knowing about if you read the code.

**Time is integer frames.** [`core/timebase.py`](vedit/core/timebase.py) holds an
exact `Fraction` frame rate; floats only appear at the ffmpeg boundary. A clip's
`src_in` is an offset into its source measured in *timeline* frames, so cutting a
25 fps source into a 30 fps timeline needs no rate conversion anywhere — the
source is addressed by time, which is the unit both ffmpeg's `trim` filter and
PyAV's `seek` already want.

**Filmstrips are sprite sheets, not files.** Each source gets one tiled image of
periodic frames at ingest, generated from the proxy because pulling 300 frames
through a 540p short-GOP file is far cheaper than decoding the original. The
timeline repaints on every frame during playback, so drawing has to be cheap:
blitting cells out of one already-loaded pixmap costs about 0.6 ms per repaint
for a full timeline. Toggle them with the **Thumbs** checkbox.

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

**The meters show what you hear, not what was decoded.** The decoder runs about
0.6 s ahead of the audio device, so metering the block it has just mixed would
put the bars ahead of the sound. Because the ring buffer is a strict FIFO, the
n-th sample written is the n-th played — so each block's peaks are stamped with
their position in the output stream and held until `processedUSecs()` says the
device has played past them. Underrun padding is discounted, or a single hiccup
would offset the meters permanently.
[`player/audio.py`](vedit/player/audio.py) does the stamping;
`MeterQueue.drain()` takes the heard position as an argument precisely so the
rule can be tested with no audio device in the process.

**Faders are read live; clip gain is baked in.** A lane or master fader writes to
a small locked structure the mix loop reads once per block, so dragging one never
rebuilds a playlist or restarts the device. Clip gain and fades travel on the
segments instead, because they are part of the edit rather than a monitoring
level — and a playlist signature check means every *other* edit, including a
video-only trim, no longer restarts audio either.

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
