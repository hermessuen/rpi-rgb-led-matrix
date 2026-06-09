#!/usr/bin/env python3
"""
L Train Times + SMS -> RGB Matrix Display

Shows next L trains at Bedford Ave (both directions stacked) on a 32x16
RGB LED matrix. When an SMS arrives via Twilio, scrolls it for 30 seconds,
then switches back to train times.

Uses train-display (double-buffered, reads frames from stdin) for the
flicker-free train display and text-scroller for scrolling SMS.

Usage:
    sudo python3 sms_display.py --ngrok-authtoken <TOKEN>
    sudo python3 sms_display.py  # if NGROK_AUTHTOKEN env var is set
"""

import os
import sys
import signal
import subprocess
import argparse
import threading
import time
import ctypes

from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from pyngrok import ngrok

try:
    from nyct_gtfs import NYCTFeed
    HAS_GTFS = True
except ImportError:
    HAS_GTFS = False
    print("WARNING: nyct-gtfs not installed.")
    print("         pip3 install nyct-gtfs\n")

app = Flask(__name__)

BEDFORD_N = "L08N"  # to Manhattan (8 Av)
BEDFORD_S = "L08S"  # to Brooklyn (Canarsie)
POLL_INTERVAL = 60  # seconds between API fetches
SMS_DURATION = 30    # seconds to show SMS
STARTUP_DELAY = 0.5  # seconds to wait after starting text-example

_lock = threading.Lock()
_display_proc = None
_mode = "train"
_sms_timer = None
_train_lines = ("Loading...", "")
_config = {}


# ── Display helpers ──────────────────────────────────────────────────────────

def _set_pdeathsig():
    """Run in the forked child (Linux): ask the kernel to send SIGTERM to this
    process when the parent (python) dies. Guarantees a renderer can never be
    orphaned holding the LED matrix, even if python is hard-killed. Must never
    raise — a failing preexec_fn would make Popen fail in the parent."""
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


def _show_scroll(text):
    """Show scrolling text using text-scroller."""
    global _display_proc
    _kill_display()
    utils_dir = _config["utils_dir"]
    repo_dir = os.path.dirname(utils_dir)
    scroller = os.path.join(utils_dir, "text-scroller")
    if not os.path.isfile(scroller) or not os.access(scroller, os.X_OK):
        return
    font = os.path.join(repo_dir, "fonts", "4x6.bdf")
    cmd = [scroller, "-f", font, "--led-rows=16", "--led-cols=32",
           f"--led-brightness={_config['brightness']}", "-s", "3", text]
    _display_proc = subprocess.Popen(cmd, preexec_fn=_set_pdeathsig)


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


def _interruptible_sleep(seconds):
    """Sleep in small increments, returning True if mode changed to non-train."""
    steps = int(seconds / 0.5)
    for _ in range(steps):
        with _lock:
            if _mode != "train":
                return True
        time.sleep(0.5)
    return False


def _train_loop():
    """Background thread: fetch train times and display both directions stacked.

    The renderer (train-display) holds the last frame on the panel and updates
    flicker-free, so we only push a new frame when the displayed text actually
    changes (new data, or after a (re)start of the renderer).
    """
    global _train_lines
    last_fetch = 0
    last_pushed = None
    proc = None
    print("[train_loop] started")

    while True:
        with _lock:
            current_mode = _mode
        if current_mode != "train":
            proc = None
            last_pushed = None
            time.sleep(0.5)
            continue

        now_time = time.time()
        if now_time - last_fetch >= POLL_INTERVAL:
            lines = _fetch_train_times()
            with _lock:
                _train_lines = lines
            last_fetch = now_time
            print(f"   Train: {_train_lines[0]} / {_train_lines[1]}")

        if proc is None or proc.poll() is not None:
            with _lock:
                if _mode != "train":
                    continue
                proc = _start_train_display()
            last_pushed = None  # force a fresh push after (re)start
            time.sleep(STARTUP_DELAY)

        # Push a frame only when the text changed (flicker-free, no clear needed).
        frame = f"{_train_lines[0]}\t{_train_lines[1]}\n"
        if frame != last_pushed:
            try:
                proc.stdin.write(frame.encode())
                proc.stdin.flush()
                last_pushed = frame
            except (BrokenPipeError, OSError):
                proc = None
                continue

        # Wake up promptly on mode change; re-evaluate poll timing each second.
        _interruptible_sleep(1)


def _switch_to_train():
    global _mode
    with _lock:
        _mode = "train"
        print(f"   Back to trains")


# ── Twilio webhook ──────────────────────────────────────────────────────────

@app.route("/sms", methods=["POST"])
def incoming_sms():
    global _mode, _sms_timer

    body = request.values.get("Body", "").strip()
    sender = request.values.get("From", "unknown")
    print(f"\n>> SMS from {sender}: {body}")

    if not body:
        resp = MessagingResponse()
        resp.message("Empty message.")
        return str(resp), 200, {"Content-Type": "application/xml"}

    with _lock:
        _mode = "sms"
        if _sms_timer:
            _sms_timer.cancel()
        _show_scroll(body)
        _sms_timer = threading.Timer(SMS_DURATION, _switch_to_train)
        _sms_timer.start()

    print(f"   Showing SMS for {SMS_DURATION}s, then back to trains.")

    resp = MessagingResponse()
    resp.message(f"Displaying for {SMS_DURATION}s: {body}")
    return str(resp), 200, {"Content-Type": "application/xml"}


@app.route("/clear", methods=["POST"])
def clear_display():
    with _lock:
        _kill_display()
    return jsonify({"status": "cleared"})


@app.route("/health", methods=["GET"])
def health():
    with _lock:
        displaying = _display_proc is not None and _display_proc.poll() is None
        mode = _mode
    return jsonify({"status": "ok", "mode": mode, "displaying": displaying})


# ── Main ────────────────────────────────────────────────────────────────────

def _cleanup(signum, frame):
    print("\nShutting down...", flush=True)
    # Hard fallback: if graceful cleanup wedges (e.g. ngrok.kill() blocks), this
    # daemon timer force-exits the whole process so Ctrl-C can never get stuck.
    watchdog = threading.Timer(6.0, lambda: os._exit(0))
    watchdog.daemon = True
    watchdog.start()
    # Best-effort, lock-free cleanup. We deliberately do NOT take _lock here: a
    # request thread could be holding it, which would deadlock the shutdown.
    # The renderer has PR_SET_PDEATHSIG, so it dies with us regardless.
    try:
        _kill_display()
    except Exception:
        pass
    try:
        ngrok.kill()
    except Exception:
        pass
    os._exit(0)


def main():
    global _config

    parser = argparse.ArgumentParser(
        description="L train times + SMS display on RGB LED matrix",
    )
    parser.add_argument("--port", type=int, default=5000,
                        help="Local HTTP port (default: 5000)")
    parser.add_argument("--ngrok-authtoken",
                        help="ngrok auth token (or set NGROK_AUTHTOKEN env var)")
    parser.add_argument("--brightness", type=int, default=50,
                        help="LED brightness 1-100 (default: 50)")
    args = parser.parse_args()

    utils_dir = os.path.dirname(os.path.abspath(__file__))
    repo_dir = os.path.dirname(utils_dir)
    _config["utils_dir"] = utils_dir
    _config["brightness"] = max(1, min(100, args.brightness))

    # Verify binaries
    train_display = os.path.join(utils_dir, "train-display")
    scroller_bin = os.path.join(utils_dir, "text-scroller")
    font_path = os.path.join(repo_dir, "fonts", "4x6.bdf")

    if not os.access(train_display, os.X_OK):
        print("WARNING: train-display not found. Run 'make' in utils/.\n")
    if not os.access(scroller_bin, os.X_OK):
        print("WARNING: text-scroller not found. Run 'make' in utils/.\n")
    if not os.path.isfile(font_path):
        print(f"WARNING: Font not found: {font_path}\n")

    token = args.ngrok_authtoken or os.environ.get("NGROK_AUTHTOKEN")
    if not token:
        print("ERROR: ngrok auth token required.")
        print("       --ngrok-authtoken <TOKEN> or set NGROK_AUTHTOKEN env var.")
        sys.exit(1)

    ngrok.set_auth_token(token)
    tunnel = ngrok.connect(args.port, "http")
    public_url = tunnel.public_url
    webhook_url = f"{public_url}/sms"

    print()
    print("=" * 62)
    print("  L Train + SMS -> RGB Matrix Display")
    print("=" * 62)
    print(f"  Local:    http://localhost:{args.port}")
    print(f"  Public:   {public_url}")
    print(f"  Webhook:  {webhook_url}")
    print(f"  Stop:     Bedford Av (L08N + L08S)")
    print(f"  Display:  Both directions stacked (next 2 trains)")
    print(f"  Poll:     every {POLL_INTERVAL}s")
    print(f"  SMS:      overrides for {SMS_DURATION}s")
    print(f"  Matrix:   32x16, font 4x6.bdf")
    print("=" * 62)
    print()
    print("  Twilio webhook (HTTP POST):")
    print(f"  -> {webhook_url}")
    print()
    print("  CTRL-C to stop.")
    print("=" * 62)
    print()

    train_thread = threading.Thread(target=_train_loop, daemon=True)
    train_thread.start()

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    app.run(host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
