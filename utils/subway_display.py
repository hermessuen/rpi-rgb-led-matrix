#!/usr/bin/env python3
"""
L Train Times -> RGB Matrix Display

Shows the next L trains at Bedford Av (both directions stacked) on a 32x16
RGB LED matrix. Updates flicker-free via the double-buffered `train-display`
renderer (only pushes a new frame when the times change).

No network server, no ngrok, no Twilio -- it just polls the MTA feed and draws.

Usage:
    sudo python3 subway_display.py [--brightness N]
"""

import os
import sys
import signal
import subprocess
import threading
import time
import argparse
import ctypes

try:
    from nyct_gtfs import NYCTFeed
    HAS_GTFS = True
except ImportError:
    HAS_GTFS = False
    print("WARNING: nyct-gtfs not installed.")
    print("         pip3 install nyct-gtfs\n")

BEDFORD_N = "L08N"  # to Manhattan (8 Av)
BEDFORD_S = "L08S"  # to Brooklyn (Canarsie)
POLL_INTERVAL = 60   # seconds between API fetches
STARTUP_DELAY = 0.5  # seconds to wait after starting the renderer

_display_proc = None
_config = {}


# ── Display helpers ──────────────────────────────────────────────────────────

def _set_pdeathsig():
    """Run in the forked child (Linux): ask the kernel to send SIGTERM to this
    process when the parent (python) dies. Guarantees the renderer can never be
    orphaned holding the LED matrix, even if python is hard-killed. Must never
    raise -- a failing preexec_fn would make Popen fail in the parent."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:
        pass


def _kill_display():
    global _display_proc
    if _display_proc and _display_proc.poll() is None:
        _display_proc.terminate()
        try:
            _display_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _display_proc.kill()
    _display_proc = None


def _start_train_display():
    """Start the double-buffered train-display process. Reads frames from stdin.

    Each frame is one line: "row0<TAB>row1\\n". Updates are flicker-free
    (SwapOnVSync), so we only push a frame when the data changes.
    """
    global _display_proc
    _kill_display()
    utils_dir = _config["utils_dir"]
    repo_dir = os.path.dirname(utils_dir)
    train_display = os.path.join(utils_dir, "train-display")
    font = os.path.join(repo_dir, "fonts", "4x6.bdf")
    cmd = [train_display, "-f", font, "--led-rows=16", "--led-cols=32",
           f"--led-brightness={_config['brightness']}"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, preexec_fn=_set_pdeathsig)
    _display_proc = proc
    return proc


# ── L train fetching ────────────────────────────────────────────────────────

def _fetch_with_timeout(timeout=15):
    """Fetch NYCTFeed in a thread with a timeout."""
    result = [None]
    def _do_fetch():
        result[0] = NYCTFeed("L")
    t = threading.Thread(target=_do_fetch)
    t.start()
    t.join(timeout)
    if t.is_alive():
        print("   MTA API timed out")
        return None
    return result[0]


def _fetch_train_times():
    """Returns (brooklyn_str, manhattan_str) for stacked display."""
    if not HAS_GTFS:
        return ("No GTFS", "")
    try:
        print("   Fetching L train data...")
        feed = _fetch_with_timeout(15)
        if feed is None:
            return ("L timeout", "")

        manhattan_trains = feed.filter_trips(line_id="L", headed_for_stop_id=BEDFORD_N, underway=True)
        brooklyn_trains = feed.filter_trips(line_id="L", headed_for_stop_id=BEDFORD_S, underway=True)

        now = time.time()

        def next_arrivals(trips, stop_id, count=2):
            times = []
            for trip in trips:
                for stu in trip.stop_time_updates:
                    if stu.stop_id == stop_id and stu.arrival:
                        mins = int((stu.arrival.timestamp() - now) / 60)
                        if mins > 6:  # only trains over 6 minutes away
                            times.append(mins)
            times.sort()
            return times[:count]

        m_times = next_arrivals(manhattan_trains, BEDFORD_N)
        b_times = next_arrivals(brooklyn_trains, BEDFORD_S)

        def fmt(times):
            if not times:
                return "--"
            return ",".join(str(t) for t in times)

        bk_str = f"B:{fmt(b_times)}"
        mn_str = f"M:{fmt(m_times)}"

        print(f"   Got: {bk_str} / {mn_str}")
        return (bk_str, mn_str)

    except Exception as e:
        print(f"   Error fetching trains: {e}")
        return ("L error", "")


# ── Main loop ────────────────────────────────────────────────────────────────

def _run():
    """Fetch train times and display both directions stacked, forever.

    The renderer holds the last frame on the panel and updates flicker-free, so
    we only push a new frame when the displayed text actually changes.
    """
    last_fetch = 0
    last_pushed = None
    lines = ("Loading...", "")
    proc = None
    print("[subway_display] running. Ctrl-C to stop.")

    while True:
        # (Re)start the renderer if it died.
        if proc is None or proc.poll() is not None:
            proc = _start_train_display()
            last_pushed = None  # force a fresh push after (re)start
            time.sleep(STARTUP_DELAY)

        # Push a frame only when the text changed (flicker-free, no clear needed).
        frame = f"{lines[0]}\t{lines[1]}\n"
        if frame != last_pushed:
            try:
                proc.stdin.write(frame.encode())
                proc.stdin.flush()
                last_pushed = frame
            except (BrokenPipeError, OSError):
                proc = None
                continue

        # Refresh the data every POLL_INTERVAL; loop straight back to push it.
        now_time = time.time()
        if now_time - last_fetch >= POLL_INTERVAL:
            lines = _fetch_train_times()
            last_fetch = now_time
            continue

        time.sleep(1)


def _cleanup(signum, frame):
    print("\nShutting down...", flush=True)
    # Hard fallback: if graceful cleanup wedges, this daemon timer force-exits
    # so Ctrl-C can never get stuck.
    watchdog = threading.Timer(6.0, lambda: os._exit(0))
    watchdog.daemon = True
    watchdog.start()
    try:
        _kill_display()  # terminates the renderer, which clears the panel
    except Exception:
        pass
    os._exit(0)


def main():
    global _config

    parser = argparse.ArgumentParser(
        description="L train times on an RGB LED matrix (no SMS/ngrok)",
    )
    parser.add_argument("--brightness", type=int, default=50,
                        help="LED brightness 1-100 (default: 50)")
    args = parser.parse_args()

    utils_dir = os.path.dirname(os.path.abspath(__file__))
    repo_dir = os.path.dirname(utils_dir)
    _config["utils_dir"] = utils_dir
    _config["brightness"] = max(1, min(100, args.brightness))

    # Verify binary + font.
    train_display = os.path.join(utils_dir, "train-display")
    font_path = os.path.join(repo_dir, "fonts", "4x6.bdf")
    if not os.access(train_display, os.X_OK):
        print("WARNING: train-display not found. Run 'make' in utils/.\n")
    if not os.path.isfile(font_path):
        print(f"WARNING: Font not found: {font_path}\n")

    print()
    print("=" * 62)
    print("  L Train -> RGB Matrix Display")
    print("=" * 62)
    print(f"  Stop:       Bedford Av (L08N + L08S)")
    print(f"  Display:    Both directions stacked (next 2 trains)")
    print(f"  Poll:       every {POLL_INTERVAL}s")
    print(f"  Brightness: {_config['brightness']}")
    print(f"  Matrix:     32x16, font 4x6.bdf")
    print("=" * 62)
    print()

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    _run()


if __name__ == "__main__":
    main()
