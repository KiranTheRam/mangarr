import pytest

from mangarr import related
from mangarr.metadata.base import SeriesMetadata
from mangarr.models import Series


def _node(id_, title, type_="MANGA", format_="MANGA", status="RELEASING"):
    return {"id": id_, "type": type_, "format": format_, "status": status,
            "title": {"romaji": title}, "coverImage": {"large": f"https://img/{id_}"}}


class FakeAniList:
    def __init__(self, edges=(), recs=(), search=()):
        self.edges, self.recs, self.search_results = list(edges), list(recs), list(search)
        self.asked = []

    async def get_related(self, anilist_id):
        self.asked.append(anilist_id)
        return self.edges, self.recs

    async def search(self, query, limit=20):
        return self.search_results

    def node_to_metadata(self, node):
        from mangarr.metadata.anilist import AniListProvider

        return AniListProvider.__new__(AniListProvider)._to_metadata(node)


class FakeMangaUpdates:
    def __init__(self, related_=(), recs=(), fail=False):
        self.related, self.recs, self.fail = list(related_), list(recs), fail

    async def get_related(self, series_id):
        if self.fail:
            raise RuntimeError("down")
        return self.related, self.recs


@pytest.fixture(autouse=True)
def clear_cache():
    related._cache.clear()


def _install(monkeypatch, al, mu):
    monkeypatch.setattr(related, "anilist", al)
    monkeypatch.setattr(related, "mangaupdates", mu)


async def test_relations_filter_merge_and_order(monkeypatch):
    al = FakeAniList(
        edges=[
            {"relationType": "SEQUEL", "node": _node(2, "Part Two")},
            {"relationType": "ADAPTATION", "node": _node(3, "The Anime", type_="ANIME")},
            {"relationType": "SIDE_STORY", "node": _node(4, "Light Novel", format_="NOVEL")},
            {"relationType": "PREQUEL", "node": _node(5, "Part Zero")},
            {"relationType": "CHARACTER", "node": _node(6, "Crossover")},
        ],
        recs=[
            {"rating": 12, "mediaRecommendation": _node(7, "Similar")},
            {"rating": -3, "mediaRecommendation": _node(8, "Voted Down")},
            {"rating": 5, "mediaRecommendation": _node(2, "Part Two")},
        ],
    )
    mu = FakeMangaUpdates(
        related_=[
            {"relation_type": "Sequel", "related_series_id": 90, "related_series_name": "Part Two"},
            {"relation_type": "Spin-Off", "related_series_id": 91, "related_series_name": "Gaiden"},
            {"relation_type": "Spin-Off", "related_series_id": 92,
             "related_series_name": "Gaiden (Novel)"},
        ],
        recs=[
            {"series_id": 80, "series_name": "Less Similar", "weight": 10},
            {"series_id": 81, "series_name": "Similar", "weight": 50},
            {"series_id": 82, "series_name": "Most Similar", "weight": 90,
             "series_image": {"url": {"original": "https://mu/82.jpg"}}},
        ],
    )
    _install(monkeypatch, al, mu)
    series = Series(id=1, title="Part One", alt_titles="", anilist_id=1, mangaupdates_id=100)
    result = await related.related_titles(series)
    assert [(r.relation, r.title, r.provider) for r in result.relations] == [
        ("Prequel", "Part Zero", "anilist"),
        ("Sequel", "Part Two", "anilist"),
        ("Spin-off", "Gaiden", "mangaupdates"),
    ]
    # related titles and repeats never show up again as recommendations
    assert [(r.title, r.provider) for r in result.recommendations] == [
        ("Similar", "anilist"), ("Most Similar", "mangaupdates"), ("Less Similar", "mangaupdates"),
    ]
    assert result.recommendations[1].cover_url == "https://mu/82.jpg"


async def test_series_without_anilist_id_is_matched_by_title(monkeypatch):
    al = FakeAniList(search=[
        SeriesMetadata(provider="anilist", provider_id="7", title="Other Thing"),
        SeriesMetadata(provider="anilist", provider_id="8", title="Frieren",
                       alt_titles=["Sousou no Frieren"]),
    ])
    _install(monkeypatch, al, FakeMangaUpdates())
    series = Series(id=2, title="Sousou no Frieren", alt_titles="", mangaupdates_id=1)
    await related.related_titles(series)
    assert al.asked == [8]


async def test_one_provider_failing_still_answers_and_both_failing_raises(monkeypatch):
    al = FakeAniList(recs=[{"rating": 1, "mediaRecommendation": _node(7, "Similar")}])
    _install(monkeypatch, al, FakeMangaUpdates(fail=True))
    series = Series(id=3, title="X", alt_titles="", anilist_id=1, mangaupdates_id=100)
    result = await related.related_titles(series)
    assert [r.title for r in result.recommendations] == ["Similar"]

    related._cache.clear()

    class Broken(FakeAniList):
        async def get_related(self, anilist_id):
            raise RuntimeError("down too")

    _install(monkeypatch, Broken(), FakeMangaUpdates(fail=True))
    with pytest.raises(RuntimeError):
        await related.related_titles(series)


async def test_results_are_cached(monkeypatch):
    al = FakeAniList()
    _install(monkeypatch, al, FakeMangaUpdates())
    series = Series(id=4, title="X", alt_titles="", anilist_id=1)
    await related.related_titles(series)
    await related.related_titles(series)
    assert al.asked == [1]
