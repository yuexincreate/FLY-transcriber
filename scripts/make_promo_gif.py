"""Render docs/promo.gif, the deck's title animation on its own, for promotion.

    uv run --with playwright python scripts/make_promo_gif.py

Opens docs/how-it-works.html in Google Chrome (headless, via Playwright's
``channel="chrome"``), keeps only the title slide's logo and name,
pauses every CSS animation and steps them frame by frame, so the GIF matches
the deck exactly whatever the machine's speed. ffmpeg joins the frames, the
last one held before the loop. Needs ffmpeg on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DECK = ROOT / "docs" / "how-it-works.html"
OUT = ROOT / "docs" / "promo.gif"

FPS = 50  # GIF delays are in 1/100 s; 50 fps gives the 0.1 s wing flaps 5 frames each
HOLD = 3.0  # seconds the finished title stays before the loop
PAD = 64  # margin around the logo and name, in CSS px
HEADROOM = 150  # extra space above (the fly's approach) and below (to keep the title centred)
GIF_WIDTH = 1200

ISOLATE = """() => {
  document.querySelectorAll('.slide:not(:first-of-type), #dots, #progress, #hint, #count, .sub')
    .forEach(e => e.remove());
  const stage = document.getElementById('stage');
  stage.style.transform = 'translate(-50%, -50%)';
  stage.style.borderRadius = '0';
  const slide = document.querySelector('.slide');
  slide.style.transition = 'none';
  slide.classList.remove('active');
  void slide.offsetWidth;
  slide.classList.add('active');
  const anims = document.getAnimations();
  anims.forEach(a => a.pause());
  window.seek = ms => anims.forEach(a => { a.currentTime = ms; });
  seek(1e6);
  const wrap = document.querySelector('.title-wrap');
  wrap.style.alignSelf = 'center';  // shrink the row to the logo and name
  const b = wrap.getBoundingClientRect();
  const end = Math.max(...anims.map(a => a.effect.getComputedTiming().endTime));
  return {x: b.left, y: b.top, w: b.width, h: b.height, end};
}"""


def main() -> int:
    frames = Path(tempfile.mkdtemp(prefix="fly-promo-"))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            page = browser.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=2)
            page.goto(DECK.as_uri() + "#1")
            box = page.evaluate(ISOLATE)
            clip = {"x": box["x"] - PAD, "y": box["y"] - PAD - HEADROOM,
                    "width": box["w"] + 2 * PAD, "height": box["h"] + 2 * (PAD + HEADROOM)}
            count = int(box["end"] / 1000 * FPS) + 1
            for i in range(count):
                page.evaluate("ms => seek(ms)", i * 1000 / FPS)
                page.screenshot(path=str(frames / f"{i:04}.png"), clip=clip)
            browser.close()

        # The last frame is repeated for the hold: the concat demuxer gives it a duration.
        listing = frames / "frames.txt"
        lines = [f"file '{frames / f'{i:04}.png'}'\nduration {1 / FPS}" for i in range(count - 1)]
        last = frames / f"{count - 1:04}.png"
        lines += [f"file '{last}'\nduration {HOLD}", f"file '{last}'"]
        listing.write_text("\n".join(lines) + "\n")
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                "-vf", f"scale={GIF_WIDTH}:-1:flags=lanczos,split[a][b];"
                       "[a]palettegen=stats_mode=full[p];[b][p]paletteuse=dither=sierra2_4a",
                "-fps_mode", "vfr", str(OUT),
            ],
            check=True,
        )
    finally:
        shutil.rmtree(frames, ignore_errors=True)
    print(f"{OUT.relative_to(ROOT)}: {count} frames + {HOLD:.0f} s hold, {OUT.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
