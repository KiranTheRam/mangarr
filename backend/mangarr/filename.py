"""Chapter/volume coverage shared by library matching and torrent selection.

Only bracket groups made entirely of a chapter/volume label carry coverage.
Scene tags such as ``(c1fi7)`` must never become chapter numbers. Spans stay
as bounds, so decimal extras are selected from tracked chapters rather than
invented by expanding an integer range.
"""

import re
from dataclasses import dataclass
from functools import lru_cache

BRACKET_GROUPS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_NUMBER = r"\d+(?:[.x]\d+)?"
_CHAPTER = r"c(?:h(?:apter)?)?[ ._\-]{0,2}"
_VOLUME = r"v(?:ol(?:ume)?)?[ ._\-]{0,2}"
# Compact v05c42 is common; letters after chapter numbers are scene tags.
_CHAPTER_TOKEN = re.compile(
    rf"(?<![a-z]){_CHAPTER}({_NUMBER})(?:\s*[-–—]\s*(?:{_CHAPTER})?({_NUMBER}))?(?![a-z0-9]|[.x]\d)", re.I
)
_VOLUME_TOKEN = re.compile(
    rf"(?<![a-z]){_VOLUME}(\d+)(?:\s*[-–—]\s*(?:{_VOLUME})?(\d+))?(?![a-bd-z0-9]|\.\d)", re.I
)
_BARE = re.compile(rf"(?<![\w.])({_NUMBER})(?:\s*[-–—]\s*({_NUMBER}))?\s*$", re.I)


def _number(value: str) -> float:
    return float(value.lower().replace("x", "."))


@dataclass(frozen=True)
class FilenameCoverage:
    chapter: tuple[float, float] | None = None
    volume: tuple[int, int] | None = None
    explicit_chapter: bool = False
    ambiguous: bool = False

    @property
    def is_range(self) -> bool:
        return any(span is not None and span[0] != span[1]
                   for span in (self.chapter, self.volume))

    @property
    def chapter_number(self) -> float | None:
        if not self.ambiguous and self.chapter and self.chapter[0] == self.chapter[1]:
            return self.chapter[0]
        return None

    @property
    def volume_number(self) -> int | None:
        if not self.ambiguous and self.volume and self.volume[0] == self.volume[1]:
            return self.volume[0]
        return None

    def covers(self, number: float, volume: int | None, *, allow_bare: bool = True) -> bool:
        if self.ambiguous:
            return False
        if self.chapter is not None:
            return ((self.explicit_chapter or allow_bare)
                    and self.chapter[0] <= number <= self.chapter[1])
        return (self.volume is not None and volume is not None
                and self.volume[0] <= volume <= self.volume[1])


@lru_cache(maxsize=4096)
def parse_filename(text: str) -> FilenameCoverage:
    untagged = BRACKET_GROUPS.sub(" ", text).strip()
    # Preserve useful labels without allowing arbitrary text in a tag to
    # masquerade as a marker. Outside labels and retained tags must agree.
    tags = [m.group()[1:-1].strip() for m in BRACKET_GROUPS.finditer(text)]
    chapter_matches = list(_CHAPTER_TOKEN.finditer(untagged))
    volume_matches = list(_VOLUME_TOKEN.finditer(untagged))
    outside_chapters = list(chapter_matches)
    outside_volumes = list(volume_matches)
    chapter_matches.extend(m for tag in tags if (m := _CHAPTER_TOKEN.fullmatch(tag)))
    volume_matches.extend(m for tag in tags if (m := _VOLUME_TOKEN.fullmatch(tag)))
    chapters = {(_number(m[1]), _number(m[2] or m[1])) for m in chapter_matches}
    volumes = {(int(m[1]), int(m[2] or m[1])) for m in volume_matches}
    explicit = bool(chapters)
    bare = _BARE.search(untagged)
    if not chapters and not volumes and bare:
        chapters.add((_number(bare[1]), _number(bare[2] or bare[1])))
    ambiguous = len(chapters) > 1 or len(volumes) > 1
    chapter = next(iter(chapters), None)
    volume = next(iter(volumes), None)
    if any(span is not None and span[1] < span[0] for span in (chapter, volume)):
        ambiguous = True
    # A second, bare number can contradict a title that looks like a marker
    # (e.g. C3 - Cube x Cursed x Curious 012).
    if explicit and bare and not any(m.end() == len(untagged) for m in outside_chapters):
        labeled_episode = re.search(
            r"\b(?:ep(?:isode)?|p(?:age)?)\.?\s*$", untagged[:bare.start()], re.I
        )
        if not labeled_episode and not any(m.end() == len(untagged) for m in outside_volumes):
            ambiguous |= (_number(bare[1]), _number(bare[2] or bare[1])) != chapter
    return FilenameCoverage(chapter, volume, explicit, ambiguous)
