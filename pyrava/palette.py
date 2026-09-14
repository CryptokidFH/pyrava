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
    min_saturation: float = 0.35,
    min_value: float = 0.15,
    diverse: bool = True,
    image=None,
) -> list[tuple[int, int, int]]:
    """Sample the dominant colours of the screen (or a supplied image).

    Returns RGB tuples, 0-255, ready to hand to ``set_zone_palette()`` or
    ``set_gradient()``.

    Requires Pillow: ``pip install "pyrava[screen]"``. Quantisation uses
    Pillow's median-cut, which is fast and avoids depending on numpy and
    scikit-learn for what is essentially palette extraction.

    Straight median-cut is a poor fit for lighting. It subdivides by pixel
    population, so on a screen dominated by one colour -- a dark editor
    theme, say -- every palette entry lands inside that one cluster and you
    get several colours differing by a couple of RGB units, which shows up
    on the lamp as one flat colour repeated. ``diverse`` (on by default)
    fixes that: it quantises to a larger pool, discards anything too dark or
    washed out, then greedily picks entries that are far apart in hue,
    breaking ties toward the more vivid ones.

    ``min_saturation`` drops washed-out pixels *before* quantising, and
    ``min_value`` drops near-black ones during selection. Both matter more
    than any post-hoc boost, since brightening or saturating a dark grey
    just gives a lighter grey. If fewer than ``n_colors`` clear the filters,
    **fewer colours are returned** rather than padding the result with murky
    ones -- three vivid colours cycled across five zones looks better than
    three vivid and two muddy. The filters are only relaxed if nothing at
    all survives them.

    ``scale`` shrinks the grab before quantising. ``image`` accepts a
    ``PIL.Image`` instead of grabbing the screen, handy for tests and for
    sampling a file. Set ``diverse=False`` for plain population-ordered
    median-cut.
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
        # getdata() is deprecated in Pillow 12 and goes away in 14, but
        # get_flattened_data() doesn't exist before then and the declared
        # floor is Pillow 9.
        reader = getattr(img, "get_flattened_data", None) or img.getdata
        kept = [
            px for px in reader()
            if colorsys.rgb_to_hsv(*[c / 255 for c in px])[1] >= min_saturation
        ]
        # Use the filtered set whenever it has at least two distinct colours
        # to work with. Requiring a full n_colors' worth would defeat the
        # point: on a screen with only a couple of vivid accents, those
        # accents are exactly what we want, even if we end up returning
        # fewer colours than asked for. Falling back to the greys there
        # would be worse than a short palette.
        if len(set(kept)) >= 2:
            filtered = Image.new("RGB", (len(kept), 1))
            filtered.putdata(kept)
            img = filtered

    if not diverse:
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

    # Quantise to a larger pool than we need, so there are candidates
    # outside the dominant cluster to choose between.
    pool = max(n_colors * 8, 32)
    quantised = img.quantize(colors=pool, method=Image.Quantize.MEDIANCUT)
    palette = quantised.getpalette()[: pool * 3]
    candidates = [
        (palette[i], palette[i + 1], palette[i + 2])
        for i in range(0, len(palette), 3)
    ]

    def _hsv(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
        h, s, v = colorsys.rgb_to_hsv(*[c / 255 for c in rgb])
        return h * 360, s, v

    def _score(rgb: tuple[int, int, int]) -> tuple[tuple[int, int, int], float, float]:
        hue, sat, val = _hsv(rgb)
        return rgb, hue, sat * val

    scored = [
        _score(rgb) for rgb in candidates
        if _hsv(rgb)[1] >= min_saturation and _hsv(rgb)[2] >= min_value
    ]
    if not scored:
        # Nothing at all clears the filters -- an all-dark or all-grey
        # screen. Relax rather than returning nothing.
        scored = [_score(rgb) for rgb in candidates if _hsv(rgb)[2] >= min_value]
    if not scored:
        scored = [_score(rgb) for rgb in candidates]
    if not scored:
        return candidates[:n_colors]

    # Greedy farthest-hue selection, seeded with the most vivid entry.
    # Candidates too close to something already chosen are skipped outright:
    # the whole point is that each zone reads as a different colour, and two
    # navies ten RGB units apart are indistinguishable on the lamp. If that
    # exhausts the candidates we return fewer colours, which set_zone_palette
    # cycles.
    min_gap = 40  # Manhattan distance in RGB

    def too_close(rgb: tuple[int, int, int]) -> bool:
        return any(
            sum(abs(x - y) for x, y in zip(rgb, c[0])) < min_gap for c in chosen
        )

    scored.sort(key=lambda t: -t[2])
    chosen = [scored[0]]
    while len(chosen) < n_colors:
        best, best_score = None, -1.0
        for cand in scored:
            if cand in chosen or too_close(cand[0]):
                continue
            gap = min(
                min(abs(cand[1] - c[1]), 360 - abs(cand[1] - c[1]))
                for c in chosen
            )
            # Distance dominates, vividness breaks ties.
            score = gap * (0.5 + cand[2])
            if score > best_score:
                best_score, best = score, cand
        if best is None:
            break  # nothing left that's visibly different
        chosen.append(best)
    return [c[0] for c in chosen]
