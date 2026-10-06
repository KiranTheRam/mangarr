"""File naming for library output. Komga/Kavita-friendly:
  {root}/{Series Title}/{Series Title} - Vol. 03 Ch. 0021.5.cbz
Templates use Python format-spec style with {series}, {volume}, {chapter}, {title}."""

import math
import re
from decimal import Decimal
from pathlib import Path

from ..util import NAME_MAX_BYTES, TITLE_MAX_BYTES, sanitize_filename, truncate_utf8

# Chapter files are named by chapter only — the volume is kept in ComicInfo.xml
# (Komga/Kavita read it there), and volume in the filename just adds noise.
DEFAULT_TEMPLATE = "{series} - Ch. {chapter:04.1f}"
DEFAULT_TEMPLATE_NO_VOLUME = "{series} - Ch. {chapter:04.1f}"

_CHAPTER_FMT = re.compile(r"\{chapter:0(\d+)\.1f\}")


def _format_chapter(template: str, chapter: float) -> str:
    """Renders {chapter:04.1f} as zero-padded but without a trailing .0 for
    whole numbers: 21 → 0021, 21.5 → 0021.5. Every decimal the source gave is
    kept (12.25 → 0012.25), because rounding to one place gives distinct
    chapters such as 12.21 and 12.24 the same filename."""

    def repl(m: re.Match) -> str:
        width = int(m.group(1))
        if not math.isfinite(chapter):
            return f"{chapter:0{width + 2}.1f}"
        if float(chapter).is_integer():
            return f"{int(chapter):0{width}d}"
        # repr keeps every decimal the source gave; Decimal spells it out
        # positionally (repr writes 0.00001 as 1e-05). The sign is handled
        # apart: int("-0") is 0, which would name -0.5 like 0.5.
        digits = format(Decimal(repr(abs(float(chapter)))), "f")
        whole, _, fraction = digits.partition(".")
        sign = "-" if chapter < 0 else ""
        return f"{sign}{int(whole):0{max(width - len(sign), 0)}d}.{fraction}"

    return _CHAPTER_FMT.sub(repl, template)


def _finish(name: str, ext: str) -> str:
    # the whole name may use the full component limit, less the extension and
    # the ".partial" suffix write_cbz adds while the archive is written
    return sanitize_filename(name, NAME_MAX_BYTES - len(f"{ext}.partial".encode())) + ext


def chapter_filename(
    template: str,
    template_no_volume: str,
    series_title: str,
    chapter: float,
    volume: int | None = None,
    title: str = "",
    ext: str = ".cbz",
) -> str:
    chosen = template if volume is not None else template_no_volume
    chosen = _format_chapter(chosen, chapter)
    name = chosen.format(
        # cap the series title, not the finished name: cutting the end off
        # would drop the chapter number and give two chapters one file
        series=truncate_utf8(series_title, TITLE_MAX_BYTES),
        volume=volume if volume is not None else 0,
        chapter=chapter,
        title=title,
    )
    return _finish(name, ext)


def volume_filename(series_title: str, volume: int, ext: str = ".cbz") -> str:
    """Name for a whole-volume archive (no per-chapter number)."""
    return _finish(f"{truncate_utf8(series_title, TITLE_MAX_BYTES)} - Vol. {volume:02d}", ext)


def series_folder(series_title: str) -> str:
    return sanitize_filename(series_title)


def chapter_path(
    root: Path,
    template: str,
    template_no_volume: str,
    series_title: str,
    folder_name: str,
    chapter: float,
    volume: int | None = None,
    title: str = "",
) -> Path:
    folder = folder_name or series_folder(series_title)
    return root / folder / chapter_filename(
        template, template_no_volume, series_title, chapter, volume, title
    )
