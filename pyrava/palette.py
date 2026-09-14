"""Colour previewing and screen sampling.

Two separable pieces, deliberately kept apart:

* **Preview helpers** (:func:`swatch`, :func:`format_palette`,
  :func:`print_palette`) are pure stdlib. Seeing the colours you're about to
  send, in the terminal, is useful for anything driving these lights, so
  these are always available.

* **Screen sampling** (:func:`dominant_colors`) needs Pillow, which is an
  optional extra: ``pip install "pyrava[screen]"``. It uses Pillow's own
  median-cut quantiser rather than k-means, which keeps the dependency to
  one library instead of pulling in numpy and scikit-learn.
"""

from __future__ import annotations

import colorsys
from typing import Sequence

__all__ = [
    "swatch",
    "format_palette",
    "print_palette",
    "dominant_colors",
    "sort_by_hue",
]

RGB = "tuple[int, int, int]"


def swatch(rgb: tuple[int, int, int], *, width: int = 4, char: str = " ") -> str:
    """A block of colour as an ANSI-escaped string.

    Uses 24-bit background colour, supported by most modern terminals
    (Windows Terminal, iTerm2, GNOME Terminal). In a terminal without
    truecolour this degrades to an approximate or ignored colour rather than
    failing.
    """
    r, g, b = (int(c) for c in rgb)
    return f"\033[48;2;{r};{g};{b}m{char * width}\033[0m"


def format_palette(
    colors: Sequence[tuple[int, int, int]],
    *,
    width: int = 4,
    show_hex: bool = True,
) -> str:
    """One line of swatches, optionally with hex values beneath.

    Returns a string rather than printing, so it composes into other output.
    """
    bar = "".join(swatch(c, width=width) for c in colors)
    if not show_hex:
        return bar
    labels = " ".join(
        f"#{r:02X}{g:02X}{b:02X}".ljust(max(width, 7)) for r, g, b in colors
    )
    return f"{bar}\n{labels}"


def print_palette(
    colors: Sequence[tuple[int, int, int]],
    *,
    width: int = 4,
    show_hex: bool = True,
) -> None:
    """Print :func:`format_palette` to stdout."""
    print(format_palette(colors, width=width, show_hex=show_hex))


def sort_by_hue(
    colors: Sequence[tuple[int, int, int]],
) -> list[tuple[int, int, int]]:
    """Order colours around the colour wheel.

    Sampled palettes come back in cluster order, which is arbitrary. For a
    gradient that matters: adjacent stops with wildly different hues blend
    through muddy intermediates. Sorting by hue keeps neighbouring stops
    close together.
    """
    def hue(rgb: tuple[int, int, int]) -> float:
        h, _s, _v = colorsys.rgb_to_hsv(*[c / 255 for c in rgb])
        return h

    return sorted((tuple(int(c) for c in x) for x in colors), key=hue)


def dominant_colors(
    n_colors: int = 5,
    *,
    scale: float = 0.15,
    min_saturation: float = 0.0,
    image=None,
) -> list[tuple[int, int, int]]:
    """Sample the dominant colours of the screen (or a supplied image).

    Returns RGB tuples, 0-255, ready to hand to ``set_zone_palette()`` or
    ``set_gradient()``.

    Requires Pillow: ``pip install "pyrava[screen]"``. Quantisation uses
    Pillow's median-cut, which is fast and avoids depending on numpy and
    scikit-learn for what is essentially palette extraction.

    ``scale`` shrinks the grab before quantising -- the default trades
    precision for speed and is plenty for five colours. ``min_saturation``
    (0-1) drops washed-out pixels before quantising; leave at 0 to keep
    everything, since filtering too aggressively on a muted screen leaves
    few pixels and yields near-duplicate colours. ``image`` accepts a
    ``PIL.Image`` instead of grabbing the screen, which is handy for tests
    and for sampling a file.
    """
    try:
        from PIL import Image, ImageGrab
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ImportError(
            "screen sampling needs Pillow: pip install \"pyrava[screen]\""
        ) from exc

    if n_colors < 1:
        raise ValueError("n_colors must be at least 1")

    img = image if image is not None else ImageGrab.grab()
    img = img.convert("RGB")

    if scale and scale != 1.0:
        w, h = img.size
        size = (max(1, int(w * scale)), max(1, int(h * scale)))
        resample = getattr(
            getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS
        )
        img = img.resize(size, resample=resample)

    if min_saturation > 0:
        kept = [
            px for px in img.getdata()
            if colorsys.rgb_to_hsv(*[c / 255 for c in px])[1] >= min_saturation
        ]
        if kept:
            filtered = Image.new("RGB", (len(kept), 1))
            filtered.putdata(kept)
            img = filtered

    quantised = img.quantize(colors=n_colors, method=Image.Quantize.MEDIANCUT)
    palette = quantised.getpalette()[: n_colors * 3]
    colors = [
        (palette[i], palette[i + 1], palette[i + 2])
        for i in range(0, len(palette), 3)
    ]

    # Order by how much of the image each colour covers, most first.
    counts = sorted(quantised.getcolors() or [], reverse=True)
    if counts and len(counts) == len(colors):
        colors = [colors[idx] for _count, idx in counts]
    return colors[:n_colors]
