#!/usr/bin/env python3
"""
Displays two lines of static text on a 32x16 RGB matrix.
Holds until killed (SIGTERM/SIGINT).

Usage:
    sudo python3 static_display.py "Line 1" "Line 2"
"""

import sys
import os
import signal
import time

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "bindings", "python"))
from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics


def main():
    if len(sys.argv) < 2:
        print("Usage: static_display.py <line1> [line2]")
        sys.exit(1)

    line1 = sys.argv[1] if len(sys.argv) > 1 else ""
    line2 = sys.argv[2] if len(sys.argv) > 2 else ""

    options = RGBMatrixOptions()
    options.rows = 16
    options.cols = 32
    options.chain_length = 1
    options.parallel = 1
    options.drop_privileges = False

    matrix = RGBMatrix(options=options)
    canvas = matrix.CreateFrameCanvas()

    font_path = os.path.join(os.path.dirname(__file__), "..", "fonts", "4x6.bdf")
    font = graphics.Font()
    font.LoadFont(font_path)

    color = graphics.Color(255, 255, 255)

    def render():
        canvas.Clear()
        graphics.DrawText(canvas, font, 0, 6, color, line1)
        graphics.DrawText(canvas, font, 0, 14, color, line2)
        matrix.SwapOnVSync(canvas)

    render()

    # Hold until killed
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
