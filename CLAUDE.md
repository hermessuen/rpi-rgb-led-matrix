# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This is a checkout of the upstream **rpi-rgb-led-matrix** C++ library (Henner Zeller, GPLv2),
customized to drive a **32x16 RGB LED panel that shows NYC L-train arrival times** at Bedford Av.
All of the project-specific work lives in `utils/`; the rest of the tree is the unmodified
library and its examples.

Hardware/runtime context that the code assumes:
- Panel is **32 cols x 16 rows**, rendered with the bitmap font `fonts/4x6.bdf`.
- Runs on a **Raspberry Pi**; driving the matrix needs GPIO, so the display binaries/scripts
  must run as **root (`sudo`)**.
- Development happens on macOS, then code is synced to the Pi and built/run there. The C++
  binaries only link on the Pi (against `lib/librgbmatrix.a`); on macOS you can only
  `g++ -fsyntax-only` and `python3 -m py_compile`.

## The subway display (the part you'll actually work on)

Three pieces in `utils/` form one pipeline:

1. **`subway_display.py`** — Python orchestrator. Polls the MTA GTFS-RT feed (`nyct-gtfs`)
   every `POLL_INTERVAL` (60s) for L trains at Bedford Av (`L08N` = Manhattan, `L08S` = Brooklyn),
   keeps the next 2 arrivals **more than 6 minutes out** per direction, formats them as
   `B:12,8` / `M:3,9`, and feeds them to the renderer. No web server, no ngrok, no Twilio —
   it just fetches and draws. The fetch/draw loop runs in the **main thread** so SIGINT lands
   immediately.

2. **`train-display.cc`** → built to `utils/train-display` — the C++ renderer. Reads **one frame
   per line from stdin**, drawn as two stacked rows. This is the load-bearing custom binary;
   read "Frame protocol" below before changing it.

3. **`run-display.sh`** — optional crash-restart supervisor (a `while` loop around the python
   process) intended to run inside tmux. Breaks the loop on a clean exit (code 0), restarts on
   any non-zero exit, and reaps an orphaned renderer before each restart.

### Frame protocol (contract between python and `train-display`)

`subway_display.py` writes, and `train-display` parses, one line per frame:

```
row0<TAB>row1\n
```

`train-display` splits on the **first tab**, clears an offscreen `FrameCanvas`, draws `row0` at
the top and `row1` one font-height below, then `SwapOnVSync()`. An empty line clears the screen.
If you change the separator, the row count, or the layout, you must change **both** files.

### Design decisions that span files (don't undo these)

- **Flicker-free updates via double buffering.** The stock `examples-api-use/text-example` draws
  directly onto the *live* canvas (`Fill()` then `DrawText()` on the buffer being scanned out), so
  every update visibly blank-and-repaints. `train-display` exists specifically to avoid that: it
  draws to an offscreen `FrameCanvas` and `SwapOnVSync()`s atomically. Do **not** route the train
  display back through `text-example`.
- **Push only on change.** The orchestrator tracks `last_pushed` and writes a frame only when the
  text actually changed. The panel holds the last swapped frame, so there's nothing to repaint
  between changes.
- **Renderer dies with its parent.** Both `subprocess.Popen` calls pass
  `preexec_fn=_set_pdeathsig`, which sets Linux `PR_SET_PDEATHSIG` so the kernel SIGTERMs the
  renderer if python dies for any reason. This prevents an orphaned `train-display` from holding
  the matrix (which would block the next start). The renderer catches SIGTERM and calls
  `matrix->Clear()`, so a clean signal turns the panel off.
- **Hang-proof shutdown.** `_cleanup` (the SIGINT/SIGTERM handler) must stay non-blocking: it does
  **not** take `_lock` (a held lock would deadlock shutdown), arms a daemon watchdog timer that
  `os._exit(0)`s after 6s, and `os._exit(0)`s itself. This is why Ctrl-C reliably stops it. If you
  add cleanup steps, keep them guarded and bounded — a blocking call here re-introduces the
  "stuck in Shutting down..." failure.

## Build

The C++ library and binaries use `make`. `config.mk` auto-selects LTO/arch flags and detects
clang vs gcc and aarch64 cross-compilation, so usually no flags are needed.

```bash
make -C lib                       # build librgbmatrix.a (pulled in automatically as a dep)
make -C utils train-display       # build just the custom renderer
make -C utils                     # build all utils binaries
make -C examples-api-use          # stock example binaries (text-example, etc.)
```

Python deps (on the Pi): `pip3 install -r utils/requirements.txt` (just `nyct-gtfs`).

## Run / stop (on the Pi, inside tmux)

```bash
# direct: runs until stopped, no auto-restart
sudo python3 utils/subway_display.py --brightness 20      # brightness 1-100, default 50

# or supervisor: auto-restarts on crash, clean Ctrl-C still stops it
BRIGHTNESS=20 ./utils/run-display.sh
```

Detach with `Ctrl-b d`; reattach with `tmux attach`; **Ctrl-C** to shut down cleanly (panel goes
dark). If something orphaned a renderer, reap it with
`sudo pkill -TERM -f train-display` (SIGTERM lets it clear the panel; `-9` leaves the last frame lit).

## Verifying changes

There is no automated test suite for the subway display. Before deploying:
- `python3 -m py_compile utils/subway_display.py` and `bash -n utils/run-display.sh` (works on macOS).
- `g++ -fsyntax-only -Iinclude utils/train-display.cc` for C++ syntax (full link only works on the Pi).
- Real validation is running it on the Pi against the live MTA feed and watching the panel.
