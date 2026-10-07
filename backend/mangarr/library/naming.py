"""File naming for library output. Komga/Kavita-friendly:
  {root}/{Series Title}/{Series Title} - Vol. 03 Ch. 0021.5.cbz
Templates use Python format-spec style with {series}, {volume}, {chapter}, {title}."""

import math
import re
from collections.abc import Callable
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


def _finish(render: Callable[[str, str], str], series: str, title: str, number: str,
            ext: str) -> str:
    """render(series, title) sanitized as a file name that fits the component
    limit, less the extension and the ".partial" suffix write_cbz adds while
    the archive is written.

    A name that is too long gives up the end of its title, then of its
    series, not its own end: that holds the chapter or volume number, and
    cutting it would give two chapters one file. Only when the template's own
    text is too long is the name cut, with number appended to keep it apart."""
    budget = NAME_MAX_BYTES - len(f"{ext}.partial".encode())
    fields = {"series": truncate_utf8(series, TITLE_MAX_BYTES), "title": title}

    def build() -> str:
        rendered = render(fields["series"], fields["title"])
        # not cut here: the fields are shortened instead
        return sanitize_filename(rendered, len(rendered.encode()) + 1)

    name = build()
    for key in ("title", "series"):
        while len(name.encode()) > budget and fields[key]:
            over = len(name.encode()) - budget
            fields[key] = truncate_utf8(fields[key], len(fields[key].encode()) - over)
            shorter = build()
            if len(shorter.encode()) >= len(name.encode()):
                fields[key] = ""  # the template doesn't show this field
                shorter = build()
            name = shorter
    if len(name.encode()) > budget:
        tag = f" {number}"
        name = sanitize_filename(name, budget - len(tag.encode())) + tag
    return name + ext


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

    def render(series: str, title: str) -> str:
        return chosen.format(
            series=series,
            volume=volume if volume is not None else 0,
            chapter=chapter,
            title=title,
        )

    return _finish(render, series_title, title, _format_chapter("{chapter:04.1f}", chapter), ext)


def volume_filename(series_title: str, volume: int, ext: str = ".cbz") -> str:
    """Name for a whole-volume archive (no per-chapter number)."""
    return _finish(lambda series, _: f"{series} - Vol. {volume:02d}", series_title, "",
                   f"Vol. {volume:02d}", ext)


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
