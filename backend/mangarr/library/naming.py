"""File naming for library output. Komga/Kavita-friendly:
  {root}/{Series Title}/{Series Title} - Vol. 03 Ch. 0021.5.cbz
Templates use Python format-spec style with {series}, {volume}, {chapter}, {title}."""

import re
from pathlib import Path

from ..util import sanitize_filename

DEFAULT_TEMPLATE = "{series} - Vol. {volume:02d} Ch. {chapter:04.1f}"
DEFAULT_TEMPLATE_NO_VOLUME = "{series} - Ch. {chapter:04.1f}"

_CHAPTER_FMT = re.compile(r"\{chapter:0(\d+)\.1f\}")


def _format_chapter(template: str, chapter: float) -> str:
    """Renders {chapter:04.1f} as zero-padded but without a trailing .0 for
    whole numbers: 21 → 0021, 21.5 → 0021.5"""

    def repl(m: re.Match) -> str:
        width = int(m.group(1))
        if float(chapter).is_integer():
            return f"{int(chapter):0{width}d}"
        return f"{chapter:0{width + 2}.1f}"

    return _CHAPTER_FMT.sub(repl, template)


def chapter_filename(
    template: str,
    template_no_volume: str,
    series_title: str,
    chapter: float,
    volume: int | None = None,
    title: str = "",
) -> str:
    chosen = template if volume is not None else template_no_volume
    chosen = _format_chapter(chosen, chapter)
    name = chosen.format(
        series=series_title,
        volume=volume if volume is not None else 0,
        chapter=chapter,
        title=title,
    )
    return sanitize_filename(name) + ".cbz"


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
