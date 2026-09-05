"""Render docs/presentation.html to PDF, and check that no slide overflows.

    uv run python docs/build_deck.py

A slide that overflows its 1280x720 frame silently clips content in the PDF and
rides up into its own heading on screen, so the check runs first and fails loudly.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

DECK = Path(__file__).resolve().parent / "presentation.html"
PDF = DECK.with_suffix(".pdf")
W, H = 1280, 720


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(DECK.as_uri())

        total = page.locator(".slide").count()
        overflowing = []
        for n in range(1, total + 1):
            page.evaluate(f"location.hash = 's{n}'")
            page.wait_for_timeout(120)
            box = page.evaluate(
                """() => {
                    const s = document.querySelector('.slide.on');
                    return { tall: s.scrollHeight - s.clientHeight,
                             wide: s.scrollWidth - s.clientWidth,
                             title: s.dataset.title };
                }"""
            )
            if box["tall"] > 1 or box["wide"] > 1:
                overflowing.append((n, box))

        page.pdf(
            path=str(PDF),
            width=f"{W}px",
            height=f"{H}px",
            print_background=True,
            prefer_css_page_size=True,
        )
        browser.close()

    for n, box in overflowing:
        print(f"  slide {n:2d}  +{box['tall']}px tall  +{box['wide']}px wide  {box['title']}")
    if errors:
        print("page errors:", errors)

    ok = not overflowing and not errors
    print(f"{total} slides · {PDF.name} written · {'OK' if ok else 'PROBLEMS ABOVE'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
