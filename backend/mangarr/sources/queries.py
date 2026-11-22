"""Bounded torrent query variants shared by search and automatic selection."""

import re
from string import Formatter

from ..titles import metadata_title_queries

_FIELDS = {"series", "chapter", "volume", "year"}
_NUMERIC_FIELDS = {"chapter", "volume"}


class _Number(str):
    def __format__(self, spec: str) -> str:
        # Pad the integer part without rounding away decimal bonus chapters.
        if not spec:
            return str(self)
        width = int(spec.removesuffix("d"))
        whole, dot, fraction = self.partition(".")
        return whole.zfill(width) + dot + fraction


def validate_templates(value: str, required: bool = False) -> None:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if (required and not lines) or len(lines) > 10 or len(value) > 2000:
        raise ValueError("Provide up to 10 torrent query templates (2000 characters); defaults cannot be empty")
    for line in lines:
        try:
            for _, field, spec, conversion in Formatter().parse(line):
                if field is None:
                    continue
                if field not in _FIELDS or conversion:
                    raise ValueError("Use only {series}, {chapter}, {volume}, and {year}")
                if spec and (field not in _NUMERIC_FIELDS or not re.fullmatch(r"0[1-9][0-9]?d?", spec)):
                    raise ValueError("Use numeric padding such as {chapter:03d} or {volume:02d}")
            line.format(series="Series", chapter=_Number("12.5"), volume=_Number("2"), year=2026)
        except (ValueError, KeyError, IndexError) as exc:
            raise ValueError(f"Invalid torrent query template: {exc}") from exc


def search_queries(series, chapters, values: dict[str, str]) -> list[str]:
    """Render at most 20 variants; skip volume/chapter tokens without metadata.

    Broad queries preserve alternate-title discovery. Explicit overrides use the
    chosen search title. Coverage is checked separately before automatic grabs.
    """
    titles = ([series.search_title] if getattr(series, "search_title", "") else
              metadata_title_queries(series))
    templates = (getattr(series, "search_templates", "") or
                 values.get("torrent_query_templates", "{series}"))
    queries = []
    for title in titles:
        for line in templates.splitlines():
            line = line.strip()
            if not line:
                continue
            fields = {field for _, field, _, _ in Formatter().parse(line) if field}
            contexts = chapters if fields & _NUMERIC_FIELDS else [None]
            for chapter in contexts:
                if "volume" in fields and (chapter is None or chapter.volume is None):
                    continue
                query = line.format(
                    series=title, year=series.year or "",
                    chapter=_Number(f"{chapter.number:g}") if chapter else "",
                    volume=_Number(str(chapter.volume)) if chapter else "",
                ).strip()
                if query and query not in queries:
                    queries.append(query)
                if len(queries) >= 20:
                    return queries
    return queries or titles[:5]
