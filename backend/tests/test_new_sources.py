import httpx
import pytest
import respx

from mangarr.sources.asura import API_URL as ASURA_API, AsuraSource
from mangarr.sources.mangaplus import API_URL as MP_API, MangaPlusError, MangaPlusSource, _xor_decrypt
from mangarr.sources.tcbscans import BASE_URL as TCB_URL, TCBScansSource

# ------------------------------------------------------------------ MangaPlus

def test_xor_decrypt_roundtrip():
    key = "a1b2c3d4"
    plain = b"the quick brown fox jumps over 13 lazy dogs"
    encrypted = _xor_decrypt(plain, key)
    assert encrypted != plain
    assert _xor_decrypt(encrypted, key) == plain  # XOR is symmetric


def test_xor_decrypt_empty_key_is_identity():
    assert _xor_decrypt(b"abc", "") == b"abc"


@respx.mock
async def test_mangaplus_error_raises():
    src = MangaPlusSource(client=httpx.AsyncClient())
    respx.get(f"{MP_API}/title_detailV3").respond(
        json={"error": {"englishPopup": {"subject": "Account Banned"}}}
    )
    with pytest.raises(MangaPlusError, match="Account Banned"):
        await src.list_chapters("100294")


@respx.mock
async def test_mangaplus_list_chapters():
    src = MangaPlusSource(client=httpx.AsyncClient())
    respx.get(f"{MP_API}/title_detailV3").respond(
        json={
            "success": {
                "titleDetailView": {
                    "chapterListGroup": [
                        {
                            "firstChapterList": [
                                {"chapterId": 1001, "name": "#1", "subTitle": "Start"},
                                {"chapterId": 1002, "name": "#2", "subTitle": ""},
                            ],
                            "lastChapterList": [
                                {"chapterId": 1050, "name": "#50", "subTitle": "Latest"},
                            ],
                        }
                    ]
                }
            }
        }
    )
    chapters = await src.list_chapters("100294")
    assert [c.number for c in chapters] == [1.0, 2.0, 50.0]
    assert chapters[0].external_id == "1001"
    assert chapters[2].title == "Latest"


@respx.mock
async def test_mangaplus_get_pages_smuggles_key():
    src = MangaPlusSource(client=httpx.AsyncClient())
    respx.get(f"{MP_API}/manga_viewer").respond(
        json={
            "success": {
                "mangaViewer": {
                    "pages": [
                        {"mangaPage": {"imageUrl": "https://cdn/x/1.jpg", "encryptionKey": "ab12"}},
                        {"bannerList": {}},  # ad page, no mangaPage → skipped
                        {"mangaPage": {"imageUrl": "https://cdn/x/2.jpg"}},  # no key
                    ]
                }
            }
        }
    )
    pages = await src.get_pages("1001")
    assert pages == ["https://cdn/x/1.jpg#mangarr_key=ab12", "https://cdn/x/2.jpg"]


# ---------------------------------------------------------------------- Asura

@respx.mock
async def test_asura_search():
    src = AsuraSource(client=httpx.AsyncClient())
    respx.get(f"{ASURA_API}/api/series").respond(
        json={
            "data": [
                {
                    "slug": "trash-of-the-counts-family",
                    "title": "Trash of the Count's Family",
                    "alt_titles": ["A", "B"],
                    "public_url": "/comics/trash-of-the-counts-family-30e93729",
                }
            ]
        }
    )
    results = await src.search_series("trash")
    assert len(results) == 1
    assert results[0].external_id == "trash-of-the-counts-family-30e93729"
    assert results[0].alt_titles == ["A", "B"]


@respx.mock
async def test_asura_list_chapters_skips_locked_premium():
    src = AsuraSource(client=httpx.AsyncClient())
    respx.get(f"{ASURA_API}/api/series/slug-abc/chapters").respond(
        json={
            "data": [
                {"number": 180, "is_premium": True, "page_count": 0},  # locked → skip
                {"number": 179, "is_premium": False, "page_count": 30},
                {"number": 178, "is_premium": True, "page_count": 25},  # premium but unlocked
            ]
        }
    )
    chapters = await src.list_chapters("slug-abc")
    assert [c.number for c in chapters] == [178.0, 179.0]
    assert chapters[1].external_id == "slug-abc|179"


@respx.mock
async def test_asura_get_pages():
    src = AsuraSource(client=httpx.AsyncClient())
    respx.get(f"{ASURA_API}/api/series/slug-abc/chapters/10").respond(
        json={
            "data": {
                "chapter": {
                    "pages": [
                        {"url": "https://cdn/10/001.webp"},
                        {"url": "https://cdn/10/002.webp"},
                    ]
                }
            }
        }
    )
    pages = await src.get_pages("slug-abc|10")
    assert pages == ["https://cdn/10/001.webp", "https://cdn/10/002.webp"]


# ------------------------------------------------------------------- TCBScans

TCB_PROJECTS = """
<html><body>
  <a href="/mangas/5/one-piece">One Piece</a>
  <a href="/mangas/4/jujutsu-kaisen">Jujutsu Kaisen</a>
  <a href="/mangas/5/one-piece">One Piece</a>
</body></html>
"""

TCB_MANGA = """
<html><body>
  <a href="/chapters/7991/one-piece-chapter-1187">Chapter 1187</a>
  <a href="/chapters/7989/one-piece-chapter-1186">Chapter 1186</a>
</body></html>
"""

TCB_CHAPTER = """
<html><body>
  <img src="/files/logo.png">
  <img class="fixed-ratio-content" src="https://cdn.tcb/op_1187_001.png">
  <img class="fixed-ratio-content" src="https://cdn.tcb/op_1187_002.png">
</body></html>
"""


@respx.mock
async def test_tcb_search_matches_catalog():
    src = TCBScansSource(client=httpx.AsyncClient())
    respx.get(f"{TCB_URL}/projects").respond(text=TCB_PROJECTS)
    results = await src.search_series("one piece")
    assert results[0].title == "One Piece"
    assert results[0].external_id == "5/one-piece"


@respx.mock
async def test_tcb_list_chapters():
    src = TCBScansSource(client=httpx.AsyncClient())
    respx.get(f"{TCB_URL}/mangas/5/one-piece").respond(text=TCB_MANGA)
    chapters = await src.list_chapters("5/one-piece")
    assert [c.number for c in chapters] == [1186.0, 1187.0]
    assert chapters[1].external_id == "7991/one-piece-chapter-1187"


@respx.mock
async def test_tcb_get_pages_filters_logo():
    src = TCBScansSource(client=httpx.AsyncClient())
    respx.get(f"{TCB_URL}/chapters/7991/one-piece-chapter-1187").respond(text=TCB_CHAPTER)
    pages = await src.get_pages("7991/one-piece-chapter-1187")
    assert pages == ["https://cdn.tcb/op_1187_001.png", "https://cdn.tcb/op_1187_002.png"]
