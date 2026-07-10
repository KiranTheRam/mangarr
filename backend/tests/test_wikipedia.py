import httpx
import pytest
import respx

from mangarr.sources.wikipedia import (
    API_URL,
    WikipediaSource,
    parse_chapter_metadata,
    parse_volume_map,
)

# Shueisha/Viz style: {{Numbered list|start=N}} inside ChapterList columns
NUMBERED_PAGE = """
== Volumes ==
{{Graphic novel list/header|Language=Japanese}}
{{Graphic novel list
| VolumeNumber    = 1
| OriginalRelDate = August 4, 2021<ref>{{Cite web|title=x}}</ref>
| OriginalISBN    = 978-4-08-882599-1
| ChapterList     =
{{Numbered list|start = 1
| {{Nihongo|"A"|あ}}
| {{Nihongo|"B"|い}}
| {{Nihongo|"C"|う}}
}}
| ChapterListCol2 =
{{Numbered list|start = 4
| {{Nihongo|"D"|え}}
| {{Nihongo|"E"|お}}
}}
| Summary = Stuff happens.
}}
{{Graphic novel list
| VolumeNumber = 2
| ChapterList  =
{{Numbered list|start = 6
| {{Nihongo|"F"}}
| {{Nihongo|"G"}}
}}
}}
"""

# Kodansha style: bullets with explicit leading numbers
BULLET_PAGE = """
{{Graphic novel list
| VolumeNumber = 1
| ChapterListCol1 =
* 1. {{Nihongo|"Deep Blue"|ディープブルー}}
* 2. {{Nihongo|"Welcoming Party"}}
| ChapterListCol2 =
* 3. {{Nihongo|"My Own Room"}}
| LineColor = FFA500
}}
"""

# Berserk style: chapter titles with no numbers at all
UNNUMBERED_PAGE = """
{{Graphic novel list
| VolumeNumber = 1
| ChapterListCol1 =
* {{Nihongo|"The Black Swordsman"|黒い剣士}}
* {{Nihongo|"The Brand"|烙印}}
}}
"""


@pytest.fixture
def source():
    return WikipediaSource(client=httpx.AsyncClient())


class TestParseVolumeMap:
    def test_numbered_list_format(self):
        assert parse_volume_map(NUMBERED_PAGE) == {
            1.0: 1, 2.0: 1, 3.0: 1, 4.0: 1, 5.0: 1, 6.0: 2, 7.0: 2,
        }

    def test_numbered_list_default_start(self):
        text = """{{Graphic novel list
| VolumeNumber = 1
| ChapterList = {{Numbered list
| "A"
| "B"
}}
}}"""
        assert parse_volume_map(text) == {1.0: 1, 2.0: 1}

    def test_bullet_format(self):
        assert parse_volume_map(BULLET_PAGE) == {1.0: 1, 2.0: 1, 3.0: 1}

    def test_unnumbered_titles_yield_nothing(self):
        # the hard rule: chapter numbers are never inferred from list
        # position — unknown stays unknown
        assert parse_volume_map(UNNUMBERED_PAGE) == {}

    def test_mixed_bullets_keep_only_explicit(self):
        text = """{{Graphic novel list
| VolumeNumber = 2
| ChapterList =
* {{Nihongo|"Unnumbered extra"}}
* 7. {{Nihongo|"Numbered"}}
}}"""
        assert parse_volume_map(text) == {7.0: 2}

    def test_prose_ranges_and_singles(self):
        text = """{{Graphic novel list
| VolumeNumber = 3
| ChapterList = Chapters 17–25
}}
{{Graphic novel list
| VolumeNumber = 4
| ChapterList = Chapter 26
}}
{{Graphic novel list
| VolumeNumber = 5
| ChapterList = Chapters 1–999
}}"""
        expected = {float(n): 3 for n in range(17, 26)}
        expected[26.0] = 4  # absurd 1–999 range distrusted, vol 5 empty
        assert parse_volume_map(text) == expected

    def test_prose_range_with_series_specific_label(self):
        # Spy × Family calls its chapters "Mission" — the label must not matter
        text = """{{Graphic novel list
| VolumeNumber = 1
| OriginalISBN = 978-4-08-882011-8
| ChapterList = *Mission: 1–5
}}"""
        assert parse_volume_map(text) == {1.0: 1, 2.0: 1, 3.0: 1, 4.0: 1, 5.0: 1}

    def test_junk_volume_numbers_skipped(self):
        text = """{{Graphic novel list
| ChapterList = {{Numbered list|start=1|A}}
}}
{{Graphic novel list
| VolumeNumber = Omnibus 1
| ChapterList = {{Numbered list|start=5|A}}
}}"""
        assert parse_volume_map(text) == {}


class TestParseChapterMetadata:
    def test_numbered_list_includes_english_titles(self):
        rows = parse_chapter_metadata(NUMBERED_PAGE)
        assert [(row.number, row.volume, row.title) for row in rows[:3]] == [
            (1.0, 1, "A"), (2.0, 1, "B"), (3.0, 1, "C"),
        ]

    def test_explicit_bonus_is_kept_unnumbered(self):
        text = """{{Graphic novel list
| VolumeNumber = 2
| ChapterList =
* 9. {{Nihongo|\"Main\"}}
* Extra: {{Nihongo|\"A Day Off\"}}
}}"""
        rows = parse_chapter_metadata(text)
        assert [(row.number, row.title, row.kind) for row in rows] == [
            (9.0, "Main", "chapter"), (None, "A Day Off", "extra"),
        ]


def _search_json(*titles):
    return {"query": {"search": [{"title": t} for t in titles]}}


def _revisions_json(title, wikitext):
    return {"query": {"pages": [
        {"title": title, "revisions": [{"slots": {"main": {"content": wikitext}}}]}
    ]}}


@respx.mock
async def test_get_volume_map_merges_multipart_lists(source):
    respx.get(API_URL, params={"list": "search"}).respond(
        json=_search_json(
            "List of Dandadan chapters (1–5)",
            "List of Dandadan chapters (6–7)",
            "Lists of Dandadan chapters",   # index page — must be rejected
            "List of Dandadan episodes",    # anime — must be rejected
        )
    )
    respx.get(API_URL, params={"titles": "List of Dandadan chapters (1–5)"}).respond(
        json=_revisions_json("x", NUMBERED_PAGE.replace("VolumeNumber = 2", "VolumeNumber = 9"))
    )
    respx.get(API_URL, params={"titles": "List of Dandadan chapters (6–7)"}).respond(
        json=_revisions_json("x", """{{Graphic novel list
| VolumeNumber = 10
| ChapterList = {{Numbered list|start=8|A|B}}
}}""")
    )
    mapping = await source.get_volume_map("Dandadan")
    assert mapping == {
        1.0: 1, 2.0: 1, 3.0: 1, 4.0: 1, 5.0: 1, 6.0: 9, 7.0: 9, 8.0: 10, 9.0: 10,
    }
    # exactly three requests: one search + two list parts, nothing for the
    # rejected titles
    assert respx.calls.call_count == 3


@respx.mock
async def test_get_volume_map_falls_back_to_article(source):
    respx.get(API_URL, params={"list": "search"}).respond(json=_search_json())
    route = respx.get(API_URL, params={"titles": "Grand Blue Dreaming"}).respond(
        json=_revisions_json("Grand Blue Dreaming", BULLET_PAGE)
    )
    mapping = await source.get_volume_map("Grand Blue Dreaming")
    assert mapping == {1.0: 1, 2.0: 1, 3.0: 1}
    assert route.called
    # the article fetch must follow redirects (stored titles can go stale)
    assert route.calls[0].request.url.params["redirects"] == "1"


@respx.mock
async def test_get_volume_map_is_cached(source):
    respx.get(API_URL, params={"list": "search"}).respond(json=_search_json())
    respx.get(API_URL, params={"titles": "Dandadan"}).respond(
        json=_revisions_json("Dandadan", NUMBERED_PAGE)
    )
    first = await source.get_volume_map("Dandadan")
    count = respx.calls.call_count
    assert await source.get_volume_map("Dandadan") == first
    assert respx.calls.call_count == count


@respx.mock
async def test_search_series_strips_disambiguator_into_alt_titles(source):
    respx.get(API_URL, params={"list": "search"}).respond(
        json=_search_json("Berserk (manga)", "Dandadan")
    )
    results = await source.search_series("Berserk")
    assert results[0].external_id == "Berserk (manga)"
    assert results[0].alt_titles == ["Berserk"]
    assert results[0].url == "https://en.wikipedia.org/wiki/Berserk_(manga)"
    assert results[1].alt_titles == []


@respx.mock
async def test_list_chapters_serves_nothing(source):
    # no routes mocked: any HTTP call would error
    assert await source.list_chapters("Dandadan") == []
