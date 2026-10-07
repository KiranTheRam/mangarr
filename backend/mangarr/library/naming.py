"""File naming for library output. Komga/Kavita-friendly:
  {root}/{Series Title}/{Series Title} - Vol. 03 Ch. 0021.5.cbz
Templates use Python format-spec style with {series}, {volume}, {chapter}, {title}."""

import logging
import math
import re
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from ..util import (
    NAME_MAX_BYTES, TITLE_MAX_BYTES, sanitize_filename, shorten_utf8, truncate_utf8,
)

log = logging.getLogger(__name__)

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


def _budget(ext: str) -> int:
    # the component limit, less the extension and the ".partial" suffix
    # write_cbz adds while the archive is written
    return NAME_MAX_BYTES - len(f"{ext}.partial".encode())


def _finish(render: Callable[[str, str], str], series: str, title: str,
            ext: str) -> str | None:
    """render(series, title) sanitized as a file name that fits _budget(ext),
    or None when the template's own text doesn't fit even with no series and
    no title.

    A name that is too long gives up the end of its title, then of its
    series, not its own end: that holds the chapter or volume number, and
    cutting it would give two chapters one file. A shortened series ends in a
    digest of the whole title (shorten_utf8), as a cut series folder does, so
    two series that start alike stay apart; the title needs none, as the
    number keeps chapters apart."""
    budget = _budget(ext)
    fields = {"series": shorten_utf8(series, TITLE_MAX_BYTES), "title": title}

    def build() -> str:
        rendered = render(fields["series"], fields["title"])
        # not cut here: the fields are shortened instead
        return sanitize_filename(rendered, len(rendered.encode()) + 1)

    def fits() -> bool:
        return len(build().encode()) <= budget

    for key, cut in (("title", truncate_utf8), ("series", shorten_utf8)):
        whole = fields[key]
        if fits() or not whole:
            continue
        fields[key] = ""
        if not fits():
            continue  # even without this field it is too long
        # the longest cut of the field that fits, measured on the finished
        # name: a cut that only removes characters sanitize_filename drops
        # anyway (spaces, "?") doesn't shorten the name. keep is a length
        # measured to fit and drop one measured not to; the gap between them
        # halves each round, so this ends, with a field that fits.
        keep, drop = 0, len(whole.encode())
        while drop - keep > 1:
            middle = (keep + drop) // 2
            fields[key] = cut(whole, middle)
            keep, drop = (middle, drop) if fits() else (keep, middle)
        fields[key] = cut(whole, keep)
    return build() + ext if fits() else None


def _cut(render: Callable[[str, str], str], ext: str) -> str:
    # last resort, for a number too long for any name: the digest a cut name
    # ends in still keeps two of them apart
    return sanitize_filename(render("", ""), _budget(ext)) + ext


def _chapter_render(template: str, chapter: float,
                    volume: int | None) -> Callable[[str, str], str]:
    chosen = _format_chapter(template, chapter)

    def render(series: str, title: str) -> str:
        return chosen.format(
            series=series,
            volume=volume if volume is not None else 0,
            chapter=chapter,
            title=title,
        )

    return render


# templates already warned about, so a library rename logs each one once
_TOO_LONG_TEMPLATES: set[str] = set()


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
    render = _chapter_render(chosen, chapter, volume)
    name = _finish(render, series_title, title, ext)
    if name is None:
        # the template's own text doesn't fit in a file name. Cutting it could
        # leave part of a chapter marker ("Ch. 1" of "Ch. 1000.25") ahead of
        # the number, and the scanner reads the first one, so the default
        # template names the file instead
        if chosen not in _TOO_LONG_TEMPLATES:
            _TOO_LONG_TEMPLATES.add(chosen)
            log.warning("naming template %r is too long for a file name; using %r",
                        chosen, DEFAULT_TEMPLATE)
        render = _chapter_render(DEFAULT_TEMPLATE, chapter, volume)
        name = _finish(render, series_title, title, ext)
    return name or _cut(render, ext)


def volume_filename(series_title: str, volume: int, ext: str = ".cbz") -> str:
    """Name for a whole-volume archive (no per-chapter number)."""

    def render(series: str, _: str) -> str:
        return f"{series} - Vol. {volume:02d}"

    return _finish(render, series_title, "", ext) or _cut(render, ext)


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
