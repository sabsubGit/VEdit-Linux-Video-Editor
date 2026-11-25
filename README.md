# vedit

A small, fast video editor for Linux that imports what you actually have.

Every Linux NLE has a disqualifying flaw. DaVinci Resolve's free Linux build
refuses H.264/AAC in MP4 — the most common format anyone has. Kdenlive gets
unstable under load, Shotcut's timeline is clumsy, Olive is perpetually alpha.

vedit does the 90% case well: **import anything, cut it, export it.**

## What it does

Four pages, mirroring how the work splits up.

- **Media** — drag and drop files or folders into the pool. Thumbnails, waveform
  peaks, filmstrips and preview proxies generate in the background. A short
  timeline under the pool takes drops so a shot can be placed where it belongs
  without changing page; `Delete` and `Backspace` work on it, while the pool's
  own `Delete` removes media from the pool.
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
- PySide6, PyAV and NumPy (installed below)

An NVIDIA GPU is used for proxy generation and optional NVENC export when
present, but nothing requires it.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

That pulls in PySide6, PyAV and NumPy. Add `".[dev]"` instead of `.` if you
want to run the tests too.

`ffmpeg` and `ffprobe` are not Python packages, so pip will not bring them in;
install them with your distribution's package manager.

## Run

```bash
.venv/bin/python -m vedit
```

Pass a project file to open it straight away:

```bash
.venv/bin/python -m vedit ~/Videos/holiday.vedit
```

## Projects

**Size and frame rate** come from the first clip you put on an empty timeline —
drop a vertical 60 fps phone clip and the project becomes vertical at 60 fps.
The status bar says so when it happens. `File ▸ Project Settings` changes it by
hand; the size can change at any time, the frame rate only while the timeline is
empty, because every cut is counted in frames at that rate.

**Preview frame rate.** `View ▸ Preview Frame Rate` limits how many pictures a
second the viewer draws during playback — full, 30, 24, 15 or 10 fps. It only
thins the preview: sound stays in sync and the export is unaffected. The choice
is remembered.

**Autosave.** Unsaved work is written every 15 seconds to
`~/.local/state/vedit/recovery/`, never over your own project file. If vedit
does not close properly, the next launch offers the work back. Unhandled errors
are logged to `~/.local/state/vedit/vedit.log`, and failed renders record their
full ffmpeg command and output there.

**Missing media.** If a file a project uses has moved, its clips stay on the
timeline in red, marked *Offline*, and play as a gap. `File ▸ Relink Missing
Media` searches a folder for every missing file by name; right-click an offline
clip to locate one file by hand, which also picks up its neighbours. A project
folder moved together with its footage reopens with nothing missing at all.

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

`View ▸ Keyboard Layout` switches between vedit's own keys and ones for hands
trained on **Premiere** or **Resolve**. The choice is remembered, and tooltips
and menus show whichever keys are in force. Only these differ:

| Action | vedit | Premiere | Resolve |
|---|---|---|---|
| Select tool | `V` | `V` | `A` |
| Cut tool | `C` | `C` | `B` |
| Split at the playhead | `X` | `Ctrl+K` | `Ctrl+B`, `Ctrl+\` |
| Ripple delete (closes the gap) | `Delete` | `Shift+Delete` | `Shift+Backspace`, `Delete` |
| Delete, leaving a gap | `Backspace` | `Delete`, `Backspace` | `Backspace` |
| Zoom in / out | `+` / `-` | `+` / `-` | `Ctrl++` / `Ctrl+-` |
| Zoom timeline to fit | `F` | `\` | `Shift+Z` |
| Snapping on / off | `S` | `S` | `N` |

Everything else is the same in all three. Where the other editor uses a key for
something vedit does not have (Premiere's `M` is a marker), vedit's meaning is
kept. The table below is the vedit layout.

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
| `S` | Snapping on / off |
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
