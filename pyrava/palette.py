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
    min_saturation: float = 0.25,
    min_value: float = 0.20,
    min_share: float = 0.01,
    hue_bins: int = 36,
    min_hue_gap: float = 25.0,
    diverse: bool = True,
    image=None,
    all_screens: bool = True,
) -> list[tuple[int, int, int]]:
    """Sample the most noticeable colours of the screen (or a supplied image).

    Returns RGB tuples, 0-255, ready to hand to ``set_zone_palette()`` or
    ``set_gradient()``. Requires Pillow: ``pip install "pyrava[screen]"``.

    Selection is by **salience**, not by area. Counting pixels picks whatever
    covers the most screen -- usually a dark editor background -- so a small
    patch of bright magenta loses to a huge field of dark navy even though
    the magenta is what you actually notice. Instead each pixel is weighted
    by ``saturation * value^2``, binned by hue, and the heaviest bins win.
    That surfaces small vivid regions and reliably returns distinct hues.

    Each returned colour is the weighted mean of its hue bin, so it's a
    genuine representative rather than the single most extreme pixel in
    that range.

    ``min_share`` ignores hue bins carrying less than this fraction of the
    total salience, which keeps a stray taskbar or desktop icon out of the
    palette. The separation is usually clean: on a sampled desktop, real
    content sat above 1% of total weight while icon-sized specks were all
    under 0.5%. Lower it to pick up genuinely small accents, or set it to 0
    to disable. Like the hue gap, it's relaxed rather than returning short.

    ``min_hue_gap`` keeps the results visually distinct: bins closer than
    this many degrees to an already-chosen colour are skipped. If that
    can't fill ``n_colors``, the gap is relaxed (halved, then dropped)
    rather than returning fewer -- a couple of similar colours in a large
    sample is fine, since re-running reshuffles which ones reach the lamp.

    ``min_saturation`` and ``min_value`` discard washed-out and near-black
    pixels before binning; both matter far more than any post-hoc boost,
    since brightening a dark grey just gives a lighter grey.

    ``scale`` shrinks the image before sampling. ``image`` accepts a
    ``PIL.Image`` instead of grabbing the screen. ``diverse=False`` falls
    back to plain population-ordered median-cut.

    Note: ``ImageGrab.grab()`` captures the primary monitor only. On a
    multi-monitor setup the colours come from whichever display Windows
    considers primary, not from everything you can see.
    """
    try:
        from PIL import Image, ImageGrab
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ImportError(
            'screen sampling needs Pillow: pip install "pyrava[screen]"'
        ) from exc

    if n_colors < 1:
        raise ValueError("n_colors must be at least 1")

    img = image if image is not None else ImageGrab.grab(all_screens=all_screens)
    img = img.convert("RGB")

    if scale and scale != 1.0:
        w, h = img.size
        size = (max(1, int(w * scale)), max(1, int(h * scale)))
        resample = getattr(
            getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS
        )
        img = img.resize(size, resample=resample)

    if not diverse:
        quantised = img.quantize(colors=n_colors, method=Image.Quantize.MEDIANCUT)
        palette = quantised.getpalette()[: n_colors * 3]
        colors = [
            (palette[i], palette[i + 1], palette[i + 2])
            for i in range(0, len(palette), 3)
        ]
        counts = sorted(quantised.getcolors() or [], reverse=True)
        if counts and len(counts) == len(colors):
            colors = [colors[idx] for _count, idx in counts]
        return colors[:n_colors]

    # getdata() is deprecated in Pillow 12 and removed in 14, but
    # get_flattened_data() doesn't exist before then and our floor is 9.
    reader = getattr(img, "get_flattened_data", None) or img.getdata

    bin_width = 360.0 / hue_bins
    weights: dict[int, float] = {}
    sums: dict[int, list[float]] = {}
    hue_sums: dict[int, float] = {}

    for px in reader():
        hue, sat, val = colorsys.rgb_to_hsv(*[c / 255 for c in px])
        if sat < min_saturation or val < min_value:
            continue
        hue *= 360
        index = int(hue // bin_width)
        # Squaring value biases toward bright colours, which is what makes a
        # small bright accent outrank a large dim background.
        weight = sat * val * val
        weights[index] = weights.get(index, 0.0) + weight
        bucket = sums.setdefault(index, [0.0, 0.0, 0.0])
        for i in range(3):
            bucket[i] += px[i] * weight
        hue_sums[index] = hue_sums.get(index, 0.0) + hue * weight

    if not weights:
        # Nothing vivid at all -- an all-grey or all-black screen. Fall back
        # to plain quantisation rather than returning nothing.
        return dominant_colors(
            n_colors, scale=1.0, diverse=False, image=img,
        )

    reps = {
        index: (
            tuple(round(sums[index][i] / weights[index]) for i in range(3)),
            hue_sums[index] / weights[index],
        )
        for index in weights
    }
    order = sorted(weights, key=lambda i: -weights[i])

    # Drop bins too small to be real content -- a taskbar icon is a few
    # dozen pixels and lands well below any sensible floor, while genuine
    # screen content sits an order of magnitude above it.
    #
    # This floor is never relaxed to reach n_colors, unlike min_hue_gap
    # below. The two rules answer different questions: the gap asks "are
    # these distinct enough to be worth separate zones", which is worth
    # bending, while the share floor asks "is this actually on the screen
    # in any meaningful amount", which isn't. Padding a palette with icon
    # colours to hit a count is exactly the behaviour this prevents. The
    # only fallback is when nothing at all clears the floor.
    if min_share > 0:
        total_weight = sum(weights.values())
        substantial = [
            i for i in order if weights[i] / total_weight >= min_share
        ]
        if substantial:
            order = substantial

    chosen: list[tuple[int, int, int]] = []
    chosen_hues: list[float] = []
    for gap in (min_hue_gap, min_hue_gap / 2, 0.0):
        for index in order:
            if len(chosen) >= n_colors:
                break
            rgb, hue = reps[index]
            if rgb in chosen:
                continue
            if gap and any(
                min(abs(hue - other), 360 - abs(hue - other)) < gap
                for other in chosen_hues
            ):
                continue
            chosen.append(rgb)
            chosen_hues.append(hue)
        if len(chosen) >= n_colors:
            break
    return chosen
