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
- **Edit** — preview viewer with a scrubber, a tool strip, and a timeline with
  three video and three audio lanes. Four tools you hold — **Select**, **Cut**,
  **Reframe** and **Zoom** — then Title and Dissolve, then Snap and Thumbnails,
  then Fit. Video clips show a filmstrip of frames and audio clips show
  their waveform, with the clip name in a band along the foot and a chain mark
  on anything still linked to its A/V partner. Razor, trim, move, ripple delete,
  clip speed, undo. Tick **Reframe** to zoom and move the picture itself — drag
  it to reposition, drag a corner to zoom, or right-click a clip for Fill Frame,
  rotate and flip. Add a **Move** and the framing glides across the shot for a
  slow push in. Right-click for a **cross dissolve** into the clip before it,
  and `Ctrl+T` writes a **title** over whatever is underneath.
- **Audio** — the same timeline with only its audio lanes, drawn twice as tall.
  Drag the line across a clip to set its level, drag the top corners inward for
  fades, or right-click to normalise. A mixer along the foot gives every lane a
  dB fader, mute and solo, and a peak meter with a held peak and a clip
  indicator, plus a master. Everything you hear here is what gets exported.
- **Render** — pick a format, a quality, a resolution and a destination, queue
  it, watch it go.

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

NVENC presets only appear when your ffmpeg actually has the encoder. Quality is
a separate choice (draft through near-lossless), as is the output size — match
the timeline, pick a named delivery size, or type your own.

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
| `M` / `R` | Mute clip audio / play the clip backwards |
| `V` / `C` | Select tool / Cut tool |
| `T` / `Z` | Reframe tool / Zoom tool |
| `Esc` | Put the tool down |
| `Ctrl+T` | Add a title at the playhead |
| `F` | Zoom timeline to fit |
| `+` / `-` | Zoom in / out |
| `Ctrl+Z` / `Ctrl+Shift+Z` | Undo / redo |
| `Ctrl+A` / `Esc` | Select all / deselect |
| `Ctrl+S` / `Ctrl+O` | Save / open project |
| `Shift+1/2/3/4` | Media / Edit / Audio / Render page |

The tools are modal: pick one and it stays until you pick another or press
`Esc`, and the status bar says what the mouse now does. **Cut** turns the
pointer into scissors and previews the frame it would split before you commit.
**Zoom** draws a box round the part of the picture that should fill the frame —
cut the clip first to zoom for just a moment of a shot. **Reframe** drags the
picture around and zooms from its corners.

**Titles** land on a lane of their own so they can be dragged along and
stretched, show their words along the clip, and can be dragged around the viewer
to sit wherever you want.

Everything on the tool strip is also in the **Edit** menu, with a **Picture**
and a **Dissolve In** submenu that act on the selected clip — or, if nothing is
selected, on whatever is under the playhead.

Mouse: click the ruler or an empty part of a lane to move the playhead — during
playback too, which jumps there and keeps playing. Drag a clip body to move it
along the timeline or up and down onto another lane, drag its edges to trim,
`Ctrl`+wheel to zoom, wheel to scroll lanes, `Shift`+wheel to scroll along the
timeline. Click a track header to mute it, `Shift`-click to lock it.

**Right-click a clip** for cut, speed (¼× to 4×, or a custom multiplier),
reverse, mute, link/unlink, enable/disable and delete. Video clips add
**Dissolve In** and a **Picture** submenu with Fill Frame, punch-in presets, Add
Move, rotate, flip and a reset that names the current framing; audio clips offer
normalise, reset gain and clear fades. Double-click a title to rewrite it.
**Right-click a track header** to mute, lock, solo, reset the lane gain, or add,
clear and delete lanes.

On the Audio page, drag a clip's volume line up and down to set its gain (hold
`Shift` to fine-adjust, and it detents at 0 dB), and drag the small handles at
its top corners inward to fade in and out. Faders respond as you drag them and
land on the undo stack once, when you let go. Double-click a fader for 0 dB.
