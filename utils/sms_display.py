#!/usr/bin/env python3
"""
L Train Times + SMS -> RGB Matrix Display

Shows next L trains at Bedford Ave (both directions stacked) on a 32x16
RGB LED matrix. When an SMS arrives via Twilio, scrolls it for 30 seconds,
then switches back to train times.

Uses text-example (reads from stdin) for static train display and
text-scroller for scrolling SMS.

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

def _kill_display():
    global _display_proc
    if _display_proc and _display_proc.poll() is None:
        _display_proc.terminate()
        try:
            _display_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _display_proc.kill()
    _display_proc = None


def _start_text_example():
    """Start a text-example process and return it. Reads from stdin."""
    global _display_proc
    _kill_display()
    repo_dir = os.path.dirname(_config["utils_dir"])
    text_example = os.path.join(repo_dir, "examples-api-use", "text-example")
    font = os.path.join(repo_dir, "fonts", "4x6.bdf")
    cmd = [text_example, "-f", font, "--led-rows=16", "--led-cols=32"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
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
    cmd = [scroller, "-f", font, "--led-rows=16", "--led-cols=32", "-s", "3", text]
    _display_proc = subprocess.Popen(cmd)


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
    """Background thread: fetch train times and display both directions stacked."""
    global _train_lines
    last_fetch = 0
    proc = None
    print("[train_loop] started")

    while True:
        with _lock:
            current_mode = _mode
        if current_mode != "train":
            proc = None
            time.sleep(1)
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
                proc = _start_text_example()
            time.sleep(STARTUP_DELAY)

        # Write both lines stacked, then empty line to clear for next refresh
        try:
            proc.stdin.write(f"{_train_lines[0]}\n{_train_lines[1]}\n".encode())
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            proc = None
            continue

        if _interruptible_sleep(POLL_INTERVAL):
            proc = None
            continue

        # Clear screen before redrawing with fresh data
        try:
            proc.stdin.write(b"\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            proc = None


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
    print("\nShutting down...")
    with _lock:
        _kill_display()
    ngrok.kill()
    sys.exit(0)


def main():
    global _config

    parser = argparse.ArgumentParser(
        description="L train times + SMS display on RGB LED matrix",
    )
    parser.add_argument("--port", type=int, default=5000,
                        help="Local HTTP port (default: 5000)")
    parser.add_argument("--ngrok-authtoken",
                        help="ngrok auth token (or set NGROK_AUTHTOKEN env var)")
    args = parser.parse_args()

    utils_dir = os.path.dirname(os.path.abspath(__file__))
    repo_dir = os.path.dirname(utils_dir)
    _config["utils_dir"] = utils_dir

    # Verify binaries
    text_example = os.path.join(repo_dir, "examples-api-use", "text-example")
    scroller_bin = os.path.join(utils_dir, "text-scroller")
    font_path = os.path.join(repo_dir, "fonts", "4x6.bdf")

    if not os.access(text_example, os.X_OK):
        print("WARNING: text-example not found.")
        print(f"         Run 'make -C examples-api-use' in the repo root.\n")
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
