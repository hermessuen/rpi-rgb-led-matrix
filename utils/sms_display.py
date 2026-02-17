#!/usr/bin/env python3
"""
L Train Times + SMS -> RGB Matrix Display

Shows next L train arrivals at Bedford Ave (both directions) on a 32x16
RGB LED matrix. When an SMS arrives via Twilio, shows it for 30 seconds,
then switches back to train times.

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
POLL_INTERVAL = 60  # seconds
SMS_DURATION = 30   # seconds

_lock = threading.Lock()
_display_proc = None
_mode = "train"
_sms_timer = None
_train_text = "Loading..."
_config = {}


def _kill_display():
    global _display_proc
    if _display_proc and _display_proc.poll() is None:
        _display_proc.terminate()
        try:
            _display_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _display_proc.kill()
    _display_proc = None


def _build_command(text):
    utils_dir = _config["utils_dir"]
    repo_dir = os.path.dirname(utils_dir)
    scroller = os.path.join(utils_dir, "text-scroller")
    if not os.path.isfile(scroller) or not os.access(scroller, os.X_OK):
        return None
    font = os.path.join(repo_dir, "fonts", "4x6.bdf")
    return [scroller, "-f", font, "--led-rows=16", "--led-cols=32", text]


def _update_display(text):
    global _display_proc
    _kill_display()
    cmd = _build_command(text)
    if cmd:
        _display_proc = subprocess.Popen(cmd)


# ── L train fetching ────────────────────────────────────────────────────────

def _fetch_train_times():
    if not HAS_GTFS:
        return "Install nyct-gtfs"
    try:
        feed = NYCTFeed("L")

        manhattan_trains = feed.filter_trips(line_id="L", headed_for_stop_id=BEDFORD_N, underway=True)
        brooklyn_trains = feed.filter_trips(line_id="L", headed_for_stop_id=BEDFORD_S, underway=True)

        now = time.time()

        def next_arrival(trips, stop_id):
            times = []
            for trip in trips:
                for stu in trip.stop_time_updates:
                    if stu.stop_id == stop_id and stu.arrival:
                        mins = int((stu.arrival.timestamp() - now) / 60)
                        if mins >= 0:
                            times.append(mins)
            times.sort()
            return times[0] if times else None

        m = next_arrival(manhattan_trains, BEDFORD_N)
        b = next_arrival(brooklyn_trains, BEDFORD_S)

        m_str = "now" if m == 0 else f"{m}m" if m is not None else "--"
        b_str = "now" if b == 0 else f"{b}m" if b is not None else "--"

        return f"Bedford Av L | Manh: {m_str} | Bklyn: {b_str}"

    except Exception as e:
        print(f"   Error fetching trains: {e}")
        return "L train: update failed"


def _train_loop():
    global _train_text
    while True:
        new_text = _fetch_train_times()
        with _lock:
            _train_text = new_text
            if _mode == "train":
                print(f"   Train: {new_text}")
                _update_display(new_text)
        time.sleep(POLL_INTERVAL)


def _switch_to_train():
    global _mode
    with _lock:
        _mode = "train"
        print(f"   Back to trains: {_train_text}")
        _update_display(_train_text)


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
        _update_display(body)
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

    scroller_bin = os.path.join(utils_dir, "text-scroller")
    font_path = os.path.join(repo_dir, "fonts", "4x6.bdf")
    if not os.access(scroller_bin, os.X_OK):
        print("WARNING: text-scroller not found. Run 'make' in utils/ first.\n")
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
