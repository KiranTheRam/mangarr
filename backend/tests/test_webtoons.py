import httpx
import respx

from mangarr.sources.webtoons import MOBILE_URL, SITE_URL, WebtoonsSource, assign_numbers, episode_number


def test_episode_number_reads_episode_markers():
    assert episode_number("Episode 0 - Prologue") == 0
    assert episode_number("Ep. 5 - Chapter 3 - Entering the Academy (2)") == 5
    assert episode_number("[Season 2] Ep. 12") == 12
    assert episode_number("Episode 108.5") == 108.5
    assert episode_number("Epilogue") is None
    assert episode_number("Prologue") is None
    # an extra's own counter is not the series' episode number
    assert episode_number("Bonus Episode 2 - Levels and Tiers") is None
    assert episode_number("Side Story - Rei (1)") is None
    # a leading episode number wins even when the title mentions a special
    assert episode_number("Ep. 40 - Special Delivery") == 40
    assert episode_number("(S2) Episode 7") == 7


def test_numbers_follow_episode_titles_and_extras_become_specials():
    titles = ["Prologue", "Episode 1", "Episode 2", "Special: Q&A", "Hiatus Notice", "Episode 3", "Episode 3"]
    assert assign_numbers(titles) == [0.0, 1.0, 2.0, 2.1, 2.2, 3.0, 3.1]


def test_bonus_counters_do_not_look_like_a_season_restart():
    titles = ["Prologue", "Episode 1", "Episode 2", "Bonus Episode 2 - Levels", "Episode 3"]
    assert assign_numbers(titles) == [0.0, 1.0, 2.0, 2.1, 3.0]


def test_long_runs_of_extras_never_reach_the_next_episode():
    titles = ["Episode 1"] + [f"Side Story - Part {i}" for i in range(12)] + ["Episode 2"]
    numbers = assign_numbers(titles)
    assert numbers[1:10] == [1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9]
    assert numbers[10:13] == [1.91, 1.92, 1.93]
    assert numbers[-1] == 2.0 and len(set(numbers)) == len(numbers)


def test_numbers_start_at_episode_zero():
    assert assign_numbers(["Episode 0 - Prologue", "Episode 1", "Episode 2"]) == [0.0, 1.0, 2.0]


def test_season_resets_fall_back_to_release_order():
    titles = ["[Season 1] Ep. 0", "[Season 1] Ep. 1", "[Season 2] Ep. 1", "[Season 2] Ep. 2"]
    assert assign_numbers(titles) == [0.0, 1.0, 2.0, 3.0]


def test_season_restart_below_the_previous_season_falls_back_too():
    titles = ["[Season 1] Ep. 0", "[Season 1] Ep. 1", "[Season 1] Ep. 2", "[Season 2] Ep. 1"]
    assert assign_numbers(titles) == [0.0, 1.0, 2.0, 3.0]


def test_untitled_episodes_are_counted_from_one():
    assert assign_numbers(["The Beginning", "The Middle", "The End"]) == [1.0, 2.0, 3.0]


SEARCH_HTML = """
<ul class="webtoon_list">
  <li><a href="https://www.webtoons.com/en/action/omniscient-reader/list?title_no=2154"
         class="link _card_item" data-title-no="2154" data-webtoon-type="WEBTOON">
      <div class="info_text"><strong class="title">Omniscient Reader</strong></div></a></li>
  <li><a href="https://www.webtoons.com/en/canvas/omniscient/list?title_no=7057"
         class="link _card_item" data-title-no="7057" data-webtoon-type="CHALLENGE">
      <div class="info_text"><strong class="title">Omniscient</strong></div></a></li>
</ul>
"""


@respx.mock
async def test_search_returns_originals_only():
    respx.get(f"{SITE_URL}/en/search").mock(return_value=httpx.Response(200, text=SEARCH_HTML))
    source = WebtoonsSource()
    results = await source.search_series("omniscient")

    assert [(r.external_id, r.title) for r in results] == [("2154", "Omniscient Reader")]
    assert results[0].url.endswith("/omniscient-reader/list?title_no=2154")
    await source._client.aclose()


async def test_search_empty_query_returns_nothing():
    source = WebtoonsSource()
    assert await source.search_series("") == []
    await source._client.aclose()


@respx.mock
async def test_list_chapters_uses_free_episode_api():
    route = respx.get(f"{MOBILE_URL}/api/v1/webtoon/2154/episodes").mock(
        return_value=httpx.Response(
            200,
            json={"result": {"episodeList": [
                {"episodeNo": 2, "episodeTitle": "Episode 1 - Beginning",
                 "viewerLink": "/en/action/omniscient-reader/episode-1/viewer?title_no=2154&episode_no=2"},
                {"episodeNo": 1, "episodeTitle": "Episode 0 - Prologue",
                 "viewerLink": "/en/action/omniscient-reader/episode-0-prologue/viewer?title_no=2154&episode_no=1"},
                {"episodeNo": 3, "episodeTitle": "Episode 2",
                 "viewerLink": "/en/action/omniscient-reader/episode-2/viewer?title_no=2154&episode_no=3"},
            ]}},
        )
    )
    source = WebtoonsSource()
    chapters = await source.list_chapters("2154")

    assert [(c.number, c.title) for c in chapters] == [(0.0, "Prologue"), (1.0, "Beginning"), (2.0, "")]
    assert chapters[1].external_id == "/en/action/omniscient-reader/episode-1/viewer?title_no=2154&episode_no=2"
    assert chapters[1].url == SITE_URL + chapters[1].external_id
    assert route.calls.last.request.url.params["pageSize"] == "99999"
    await source._client.aclose()


VIEWER_HTML = """
<div id="_imageList">
  <img src="bg.png" data-url="https://webtoon-phinf.pstatic.net/a/001.jpg?type=q90" class="_images" alt="">
  <img src="bg.png" data-url="https://webtoon-phinf.pstatic.net/a/002.jpg?type=q90" class="_images" alt="">
</div>
<img data-url="https://webtoon-phinf.pstatic.net/thumb/recommended.jpg?type=q90" class="thumb">
"""


@respx.mock
async def test_get_pages_returns_viewer_strips_in_order():
    link = "/en/action/omniscient-reader/episode-1/viewer?title_no=2154&episode_no=2"
    respx.get(SITE_URL + link).mock(return_value=httpx.Response(200, text=VIEWER_HTML))
    source = WebtoonsSource()

    assert await source.get_pages(link) == [
        "https://webtoon-phinf.pstatic.net/a/001.jpg?type=q90",
        "https://webtoon-phinf.pstatic.net/a/002.jpg?type=q90",
    ]
    assert source.image_headers() == {"Referer": f"{SITE_URL}/"}
    await source._client.aclose()
