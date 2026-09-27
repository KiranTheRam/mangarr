"""Related titles (sequels, side stories, spin-offs …) and recommendations
for a library series, from AniList with MangaUpdates filling in.

AniList has covers and clean relation types but only knows a series by its
AniList id, which series added from MangaUpdates lack — those are matched
by title. MangaUpdates adds manga spin-offs AniList doesn't track and its
own community recommendations."""

import logging
import time
from dataclasses import dataclass, field

from .metadata.anilist import STATUS_MAP
from .metadata.anilist import provider as anilist
from .metadata.mangaupdates import provider as mangaupdates
from .models import Series
from .titles import split_alt_titles, title_queries
from .util import normalize_title

log = logging.getLogger(__name__)

CACHE_TTL = 6 * 3600.0
MAX_RECOMMENDATIONS = 18

# AniList relation types worth offering, in display order. ADAPTATION points
# at anime and CHARACTER at anything sharing a cameo — neither is a manga to
# add.
_ANILIST_RELATIONS = {
    "PREQUEL": "Prequel",
    "SEQUEL": "Sequel",
    "PARENT": "Parent story",
    "SIDE_STORY": "Side story",
    "SPIN_OFF": "Spin-off",
    "ALTERNATIVE": "Alternative version",
    "SOURCE": "Source",
    "SUMMARY": "Summary",
    "COMPILATION": "Compilation",
    "CONTAINS": "Contains",
    "OTHER": "Other",
}
_RELATION_ORDER = {label: i for i, label in enumerate(_ANILIST_RELATIONS.values())}
# MangaUpdates relation names, mapped onto the AniList labels above
_MU_RELATIONS = {
    "prequel": "Prequel",
    "sequel": "Sequel",
    "main story": "Parent story",
    "side story": "Side story",
    "spin-off": "Spin-off",
    "alternate story": "Alternative version",
    "alternate version": "Alternative version",
    "adapted from": "Source",
}


@dataclass
class RelatedTitle:
    provider: str
    provider_id: str
    title: str
    alt_titles: list[str] = field(default_factory=list)
    cover_url: str = ""
    year: int | None = None
    status: str = "unknown"
    format: str = ""
    relation: str = ""

    @property
    def title_keys(self) -> set[str]:
        return {n for t in [self.title, *self.alt_titles] if (n := normalize_title(t))}


@dataclass
class Related:
    relations: list[RelatedTitle]
    recommendations: list[RelatedTitle]


# series id → (expiry, result); the relation graph changes rarely and the
# series page asks every time it opens
_cache: dict[int, tuple[float, Related]] = {}


def _is_prose(format_: str, title: str) -> bool:
    # light novels are MANGA-typed on AniList; MangaUpdates only says so in
    # the name ("… (Novel)")
    return format_ == "NOVEL" or title.rstrip().lower().endswith("(novel)")


def _from_anilist_node(node: dict, relation: str) -> RelatedTitle | None:
    if node.get("type") != "MANGA" or _is_prose(node.get("format") or "", ""):
        return None
    meta = anilist.node_to_metadata(node)
    return RelatedTitle(
        provider="anilist",
        provider_id=meta.provider_id,
        title=meta.title,
        alt_titles=meta.alt_titles,
        cover_url=meta.cover_url,
        year=meta.year,
        status=STATUS_MAP.get(node.get("status") or "", "unknown"),
        format=node.get("format") or "",
        relation=relation,
    )


async def _resolve_anilist_id(series: Series) -> int | None:
    """The series' AniList id, or its AniList twin found by title."""
    if series.anilist_id is not None:
        return series.anilist_id
    wanted = {
        n for t in [series.title, *split_alt_titles(series.alt_titles)]
        if (n := normalize_title(t))
    }
    for query in title_queries(series.title, split_alt_titles(series.alt_titles), limit=2):
        for cand in await anilist.search(query, limit=5):
            keys = {n for t in [cand.title, *cand.alt_titles] if (n := normalize_title(t))}
            if wanted & keys:
                return int(cand.provider_id)
    return None


async def _anilist_related(series: Series) -> tuple[list[RelatedTitle], list[RelatedTitle]]:
    anilist_id = await _resolve_anilist_id(series)
    if anilist_id is None:
        return [], []
    edges, rec_nodes = await anilist.get_related(anilist_id)
    relations = []
    for edge in edges:
        label = _ANILIST_RELATIONS.get(edge.get("relationType") or "")
        if label and (item := _from_anilist_node(edge.get("node") or {}, label)):
            relations.append(item)
    recommendations = []
    for rec in rec_nodes:
        node = rec.get("mediaRecommendation")
        # a zero or negative rating means users voted the pairing down
        if node and (rec.get("rating") or 0) > 0:
            if item := _from_anilist_node(node, "Recommended"):
                recommendations.append(item)
    return relations, recommendations


async def _mangaupdates_related(series: Series) -> tuple[list[RelatedTitle], list[RelatedTitle]]:
    if series.mangaupdates_id is None:
        return [], []
    raw_related, raw_recs = await mangaupdates.get_related(series.mangaupdates_id)
    relations = []
    for rel in raw_related:
        label = _MU_RELATIONS.get((rel.get("relation_type") or "").lower())
        name = rel.get("related_series_name") or ""
        if label and name and rel.get("related_series_id") and not _is_prose("", name):
            relations.append(RelatedTitle(
                provider="mangaupdates",
                provider_id=str(rel["related_series_id"]),
                title=name,
                relation=label,
            ))
    recommendations = []
    for rec in sorted(raw_recs, key=lambda r: -(r.get("weight") or 0)):
        name = rec.get("series_name") or ""
        if not name or not rec.get("series_id") or _is_prose("", name):
            continue
        image = ((rec.get("series_image") or {}).get("url")) or {}
        recommendations.append(RelatedTitle(
            provider="mangaupdates",
            provider_id=str(rec["series_id"]),
            title=name,
            cover_url=image.get("original") or image.get("thumb") or "",
            relation="Recommended",
        ))
    return relations, recommendations


def _merge(primary: list[RelatedTitle], extra: list[RelatedTitle],
           exclude: set[str]) -> list[RelatedTitle]:
    """primary, then extra entries naming a title not already present."""
    seen = set(exclude)
    out: list[RelatedTitle] = []
    for item in [*primary, *extra]:
        keys = item.title_keys
        if keys & seen:
            continue
        seen |= keys
        out.append(item)
    return out


async def related_titles(series: Series) -> Related:
    """Relations and recommendations for the series, cached for a while.
    One provider failing still returns the other's; both failing raises."""
    cached = _cache.get(series.id)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    results = []
    errors = []
    for fetch in (_anilist_related, _mangaupdates_related):
        try:
            results.append(await fetch(series))
        except Exception as exc:
            log.warning("related titles via %s failed for %r: %s",
                        fetch.__name__, series.title, exc)
            errors.append(exc)
            results.append(([], []))
    if len(errors) == 2:
        raise errors[0]
    (al_rel, al_recs), (mu_rel, mu_recs) = results
    own = {n for t in [series.title, *split_alt_titles(series.alt_titles)]
           if (n := normalize_title(t))}
    relations = _merge(al_rel, mu_rel, own)
    relations.sort(key=lambda r: _RELATION_ORDER.get(r.relation, len(_RELATION_ORDER)))
    related_keys = set().union(*(r.title_keys for r in relations)) if relations else set()
    recommendations = _merge(al_recs, mu_recs, own | related_keys)[:MAX_RECOMMENDATIONS]
    result = Related(relations=relations, recommendations=recommendations)
    # a partial answer is only cached briefly so the failed half gets retried
    ttl = CACHE_TTL if not errors else 600.0
    _cache[series.id] = (time.monotonic() + ttl, result)
    return result


