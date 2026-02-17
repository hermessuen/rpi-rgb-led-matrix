#!/usr/bin/env python3
"""
SMS-to-RGB-Matrix Display Server

Receives incoming SMS messages via a Twilio webhook (exposed through ngrok)
and scrolls the message text on a 32x16 RGB LED matrix using text-scroller.

Usage:
    sudo python3 sms_display.py --ngrok-authtoken <TOKEN>
    sudo python3 sms_display.py  # if NGROK_AUTHTOKEN env var is set

Then configure the printed webhook URL in your Twilio phone number settings
as the "A MESSAGE COMES IN" webhook (HTTP POST).
"""

import os
import sys
import signal
import subprocess
import argparse

from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from pyngrok import ngrok, conf as ngrok_conf

app = Flask(__name__)

# Tracks the currently running display subprocess
_display_proc = None

# Populated by main() before the server starts
_config = {}


def _kill_display():
    """Terminate any running display process."""
    global _display_proc
    if _display_proc and _display_proc.poll() is None:
        _display_proc.terminate()
        try:
            _display_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _display_proc.kill()
    _display_proc = None


def _build_command(text):
    """Build the text-scroller command for the 32x16 matrix."""
    utils_dir = _config["utils_dir"]
    repo_dir = os.path.dirname(utils_dir)
    text_scroller_bin = os.path.join(utils_dir, "text-scroller")

    if not os.path.isfile(text_scroller_bin) or not os.access(text_scroller_bin, os.X_OK):
        return None

    font_path = os.path.join(repo_dir, "fonts", "4x6.bdf")

    cmd = [
        text_scroller_bin,
        "-f", font_path,
        "--led-rows=16",
        "--led-cols=32",
        text,
    ]
    return cmd


# ── Twilio webhook endpoint ─────────────────────────────────────────────────

@app.route("/sms", methods=["POST"])
def incoming_sms():
    """Handle an incoming Twilio SMS webhook."""
    global _display_proc

    body = request.values.get("Body", "").strip()
    sender = request.values.get("From", "unknown")

    print(f"\n>> SMS from {sender}: {body}")

    if not body:
        resp = MessagingResponse()
        resp.message("Empty message — nothing to display.")
        return str(resp), 200, {"Content-Type": "application/xml"}

    # Kill previous display
    _kill_display()

    cmd = _build_command(body)
    if cmd is None:
        print("ERROR: text-scroller binary not found in utils/.")
        print("       Run 'make' in the utils/ directory first.")
        resp = MessagingResponse()
        resp.message("Server error: display binary not found.")
        return str(resp), 200, {"Content-Type": "application/xml"}

    print(f"   Running: {' '.join(cmd)}")
    _display_proc = subprocess.Popen(cmd)

    resp = MessagingResponse()
    resp.message(f"Displaying: {body}")
    return str(resp), 200, {"Content-Type": "application/xml"}


@app.route("/clear", methods=["POST"])
def clear_display():
    """Clear the matrix (kills the display process)."""
    _kill_display()
    return jsonify({"status": "cleared"})


@app.route("/health", methods=["GET"])
def health():
    """Simple health check."""
    displaying = _display_proc is not None and _display_proc.poll() is None
    return jsonify({"status": "ok", "displaying": displaying})


# ── Main ─────────────────────────────────────────────────────────────────────

def _cleanup(signum, frame):
    print("\nShutting down...")
    _kill_display()
    ngrok.kill()
    sys.exit(0)


def main():
    global _config

    parser = argparse.ArgumentParser(
        description="SMS → RGB LED Matrix display server (Twilio + ngrok)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  sudo python3 sms_display.py --ngrok-authtoken <TOKEN>
  sudo NGROK_AUTHTOKEN=<TOKEN> python3 sms_display.py
  sudo python3 sms_display.py --port 8080
""",
    )

    parser.add_argument("--port", type=int, default=5000,
                        help="Local HTTP port (default: 5000)")
    parser.add_argument("--ngrok-authtoken",
                        help="ngrok auth token (or set NGROK_AUTHTOKEN env var)")

    args = parser.parse_args()

    # ── Resolve paths ────────────────────────────────────────────────────
    utils_dir = os.path.dirname(os.path.abspath(__file__))
    repo_dir = os.path.dirname(utils_dir)

    _config.update({
        "utils_dir": utils_dir,
    })

    # ── Verify binary and font exist ─────────────────────────────────────
    scroller_bin = os.path.join(utils_dir, "text-scroller")
    font_path = os.path.join(repo_dir, "fonts", "4x6.bdf")

    if not os.access(scroller_bin, os.X_OK):
        print("WARNING: text-scroller binary not found. Run 'make' in utils/ first.")
        print(f"         Looked in: {utils_dir}\n")

    if not os.path.isfile(font_path):
        print(f"WARNING: Font file not found: {font_path}\n")

    # ── Start ngrok ──────────────────────────────────────────────────────
    token = args.ngrok_authtoken or os.environ.get("NGROK_AUTHTOKEN")
    if not token:
        print("ERROR: ngrok auth token required.")
        print("       Pass --ngrok-authtoken <TOKEN> or set NGROK_AUTHTOKEN env var.")
        print("       Get a free token at https://dashboard.ngrok.com/signup")
        sys.exit(1)

    ngrok.set_auth_token(token)
    tunnel = ngrok.connect(args.port, "http")
    public_url = tunnel.public_url
    webhook_url = f"{public_url}/sms"

    print()
    print("=" * 62)
    print("  SMS → RGB Matrix Display Server")
    print("=" * 62)
    print(f"  Local:    http://localhost:{args.port}")
    print(f"  Public:   {public_url}")
    print(f"  Webhook:  {webhook_url}")
    print(f"  Font:     4x6.bdf")
    print(f"  Matrix:   32x16 (--led-cols=32 --led-rows=16)")
    print("=" * 62)
    print()
    print("  Configure your Twilio phone number webhook (HTTP POST):")
    print(f"  → {webhook_url}")
    print()
    print("  Send an SMS to your Twilio number to scroll it!")
    print("  Press CTRL-C to stop.")
    print("=" * 62)
    print()

    # ── Graceful shutdown ────────────────────────────────────────────────
    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    # ── Run Flask ────────────────────────────────────────────────────────
    app.run(host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
