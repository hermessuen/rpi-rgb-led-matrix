// -*- mode: c++; c-basic-offset: 2; indent-tabs-mode: nil; -*-
// Persistent two-line text display with flicker-free (double-buffered) updates.
//
// Reads one "frame" per line from stdin. A frame is two rows separated by a
// TAB character:   row0 \t row1 \n
// An empty line clears the screen. Because each frame is drawn to an offscreen
// FrameCanvas and pushed with SwapOnVSync(), updates are atomic: the panel
// never shows a blank/half-drawn frame, so there is no full-screen flash when
// the train times change.
//
// This code is public domain
// (but note, that the led-matrix library this depends on is GPL v2)

#include "led-matrix.h"
#include "graphics.h"

#include <getopt.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

using namespace rgb_matrix;

static volatile bool interrupt_received = false;
static void InterruptHandler(int signo) { interrupt_received = true; }

static int usage(const char *progname) {
  fprintf(stderr, "usage: %s [options]\n", progname);
  fprintf(stderr,
          "Reads one frame per line from stdin: \"row0<TAB>row1\\n\".\n"
          "Empty line clears the screen. Updates are double-buffered.\n");
  fprintf(stderr, "Options:\n");
  fprintf(stderr,
          "\t-f <font-file>    : Use given font (required).\n"
          "\t-x <x-origin>     : X-origin of text (Default: 0)\n"
          "\t-y <y-origin>     : Y-origin of first row (Default: 0)\n"
          "\t-S <spacing>      : Spacing pixels between letters (Default: 0)\n"
          "\t-C <r,g,b>        : Text color. Default 255,255,0\n"
          "\n");
  rgb_matrix::PrintMatrixFlags(stderr);
  return 1;
}

static bool parseColor(Color *c, const char *str) {
  return sscanf(str, "%hhu,%hhu,%hhu", &c->r, &c->g, &c->b) == 3;
}

int main(int argc, char *argv[]) {
  RGBMatrix::Options matrix_options;
  rgb_matrix::RuntimeOptions runtime_opt;
  if (!rgb_matrix::ParseOptionsFromFlags(&argc, &argv,
                                         &matrix_options, &runtime_opt)) {
    return usage(argv[0]);
  }

  Color color(255, 255, 0);
  const char *bdf_font_file = NULL;
  int x_orig = 0;
  int y_orig = 0;
  int letter_spacing = 0;

  int opt;
  while ((opt = getopt(argc, argv, "x:y:f:C:S:")) != -1) {
    switch (opt) {
    case 'x': x_orig = atoi(optarg); break;
    case 'y': y_orig = atoi(optarg); break;
    case 'f': bdf_font_file = strdup(optarg); break;
    case 'S': letter_spacing = atoi(optarg); break;
    case 'C':
      if (!parseColor(&color, optarg)) {
        fprintf(stderr, "Invalid color spec: %s\n", optarg);
        return usage(argv[0]);
      }
      break;
    default:
      return usage(argv[0]);
    }
  }

  if (bdf_font_file == NULL) {
    fprintf(stderr, "Need to specify BDF font-file with -f\n");
    return usage(argv[0]);
  }

  rgb_matrix::Font font;
  if (!font.LoadFont(bdf_font_file)) {
    fprintf(stderr, "Couldn't load font '%s'\n", bdf_font_file);
    return 1;
  }

  RGBMatrix *matrix = RGBMatrix::CreateFromOptions(matrix_options, runtime_opt);
  if (matrix == NULL)
    return 1;

  // Offscreen buffer we draw into, then atomically swap onto the panel.
  FrameCanvas *offscreen = matrix->CreateFrameCanvas();

  signal(SIGTERM, InterruptHandler);
  signal(SIGINT, InterruptHandler);

  char line[1024];
  while (!interrupt_received && fgets(line, sizeof(line), stdin)) {
    // Strip trailing newline.
    size_t len = strlen(line);
    if (len > 0 && line[len - 1] == '\n') line[--len] = '\0';

    // Split into (at most) two rows on the first TAB.
    const char *row0 = line;
    const char *row1 = "";
    char *tab = strchr(line, '\t');
    if (tab) {
      *tab = '\0';
      row1 = tab + 1;
    }

    offscreen->Clear();
    if (row0[0] != '\0') {
      rgb_matrix::DrawText(offscreen, font, x_orig,
                           y_orig + font.baseline(),
                           color, NULL, row0, letter_spacing);
    }
    if (row1[0] != '\0') {
      rgb_matrix::DrawText(offscreen, font, x_orig,
                           y_orig + font.height() + font.baseline(),
                           color, NULL, row1, letter_spacing);
    }
    // Atomic, flicker-free swap onto the panel.
    offscreen = matrix->SwapOnVSync(offscreen);
  }

  matrix->Clear();
  delete matrix;
  return 0;
}
