"""Import lists: provider parsing and the sync bookkeeping that keeps each
entry from being added more than once."""

import json
from contextlib import asynccontextmanager

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from mangarr import adding, import_lists
from mangarr.api.import_lists import create_import_list, preview_import_list, update_import_list
from mangarr.api.settings import MASK
from mangarr.import_lists import (
    MAL_API_URL,
    ImportListError,
    ListEntry,
    fetch_entries,
    load_list,
    mangaupdates_id_from_link,
    sync_list,
)
from mangarr.metadata.anilist import API_URL as ANILIST_URL
from mangarr.metadata.base import SeriesMetadata
from mangarr.metadata.mangaupdates import API_URL as MU_URL
from mangarr.models import Base, ImportList, RootFolder, Series
from mangarr.schemas import ImportListIn


@pytest.fixture
async def maker(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def scope():
        async with maker() as session:
            yield session

    monkeypatch.setattr(import_lists, "session_scope", scope)
    yield maker
    await engine.dispose()


@pytest.fixture
def refreshes(monkeypatch):
    started: list[tuple[list[int], bool]] = []
    monkeypatch.setattr(import_lists, "start_refreshes",
                        lambda ids, grab_missing=False: started.append((ids, grab_missing)))
    return started


class FakeMetadata:
    def __init__(self, name):
        self.name = name

    async def get_series(self, provider_id):
        if provider_id == "404":
            return None
        return SeriesMetadata(provider=self.name, provider_id=provider_id,
                              title=f"{self.name} {provider_id}")


@pytest.fixture
def providers(monkeypatch):
    monkeypatch.setattr(adding, "anilist", FakeMetadata("anilist"))
    monkeypatch.setattr(adding, "mangaupdates", FakeMetadata("mangaupdates"))


def _serve(monkeypatch, entries: list[ListEntry]):
    served = {"entries": entries}

    async def fetch(kind, config):
        return list(served["entries"])

    monkeypatch.setattr(import_lists, "fetch_entries", fetch)
    return served


async def _list(maker, **kwargs) -> int:
    async with maker() as session:
        root = RootFolder(path="/manga")
        session.add(root)
        await session.flush()
        lst = ImportList(name="mine", kind="anilist", root_folder_id=root.id,
                         config=json.dumps({"username": "me"}), **kwargs)
        session.add(lst)
        await session.commit()
        return lst.id


async def _sync(maker, list_id):
    async with maker() as session:
        return await sync_list(session, await load_list(session, list_id))


async def test_sync_adds_new_entries_once(maker, providers, refreshes, monkeypatch):
    served = _serve(monkeypatch, [
        ListEntry(key="anilist:1", title="One", anilist_id=1),
        ListEntry(key="anilist:2", title="Two", anilist_id=2, mangaupdates_id=22),
    ])
    list_id = await _list(maker, monitored=False, search_now=True)
    result = await _sync(maker, list_id)
    assert (result.fetched, result.added, result.existing, result.failed) == (2, 2, 0, 0)
    assert len(refreshes) == 1 and refreshes[0][1] is True
    async with maker() as session:
        series = (await session.execute(select(Series))).scalars().all()
        # the list's own site is the identity when it is a metadata provider
        assert sorted(s.anilist_id for s in series) == [1, 2]
        assert all(s.monitored is False for s in series)
        # deleting a series a list added must not bring it back next sync
        await session.delete(series[0])
        await session.commit()

    served["entries"].append(ListEntry(key="anilist:3", title="Three", anilist_id=3))
    result = await _sync(maker, list_id)
    assert (result.added, result.existing, result.failed) == (1, 0, 0)
    async with maker() as session:
        assert len((await session.execute(select(Series))).scalars().all()) == 2


async def test_entries_already_in_library_are_recorded_not_added(
    maker, providers, refreshes, monkeypatch
):
    _serve(monkeypatch, [ListEntry(key="myanimelist:9", title="Attack on Titan")])
    list_id = await _list(maker)
    async with maker() as session:
        session.add(Series(title="Shingeki no Kyojin", alt_titles="Attack on Titan",
                           mangaupdates_id=5))
        await session.commit()
    result = await _sync(maker, list_id)
    assert (result.added, result.existing) == (0, 1)
    assert refreshes == []
    async with maker() as session:
        lst = await load_list(session, list_id)
        assert [(e.status, e.series_id is not None) for e in lst.entries] == [("existing", True)]


async def test_failed_entries_retry_on_the_next_sync(maker, providers, refreshes, monkeypatch):
    _serve(monkeypatch, [ListEntry(key="anilist:404", title="Gone", anilist_id=404)])
    list_id = await _list(maker)
    result = await _sync(maker, list_id)
    assert result.failed == 1
    async with maker() as session:
        entry = (await load_list(session, list_id)).entries[0]
        assert entry.status == "failed" and "not found" in entry.detail
    # the provider knows it now
    _serve(monkeypatch, [ListEntry(key="anilist:404", title="Gone", mangaupdates_id=7)])
    result = await _sync(maker, list_id)
    assert (result.added, result.failed) == (1, 0)
    async with maker() as session:
        entries = (await load_list(session, list_id)).entries
        assert [e.status for e in entries] == ["added"]


async def test_entry_without_ids_falls_back_to_a_title_search(
    maker, providers, refreshes, monkeypatch
):
    _serve(monkeypatch, [ListEntry(key="myanimelist:1", title="Obscure Title")])

    async def search(query, limit=20):
        return [
            SeriesMetadata(provider="mangaupdates", provider_id="50", title="Obscure Title Deluxe"),
            SeriesMetadata(provider="mangaupdates", provider_id="51", title="Obscure-Title"),
        ]

    monkeypatch.setattr(import_lists.mangaupdates, "search", search)
    list_id = await _list(maker)
    assert (await _sync(maker, list_id)).added == 1
    async with maker() as session:
        assert (await session.execute(select(Series.mangaupdates_id))).scalar_one() == 51


async def test_fetch_error_is_recorded_on_the_list(maker, refreshes, monkeypatch):
    async def fetch(kind, config):
        raise ImportListError("AniList: Private User")

    monkeypatch.setattr(import_lists, "fetch_entries", fetch)
    list_id = await _list(maker)
    with pytest.raises(ImportListError):
        await _sync(maker, list_id)
    async with maker() as session:
        assert (await session.get(ImportList, list_id)).last_error == "AniList: Private User"


async def test_saved_password_survives_a_masked_update(maker):
    async with maker() as session:
        session.add(RootFolder(path="/manga"))
        await session.commit()
        body = ImportListIn(name="mu", kind="mangaupdates", username="me", password="hunter2",
                            root_folder_id=1)
        out = await create_import_list(body, session)
        assert out.password == MASK
        body.password = MASK
        body.statuses = ["0", "2"]
        await update_import_list(out.id, body, session)
        lst = await session.get(ImportList, out.id)
        config = json.loads(lst.config)
        assert config["password"] == "hunter2" and config["statuses"] == ["0", "2"]


async def test_preview_does_not_add_anything(maker, providers, monkeypatch):
    _serve(monkeypatch, [ListEntry(key="anilist:1", title="One", anilist_id=1)])
    async with maker() as session:
        session.add(RootFolder(path="/manga"))
        await session.commit()
        items = await preview_import_list(
            ImportListIn(name="x", kind="anilist", username="me", root_folder_id=1), None, session
        )
        assert [i.action for i in items] == ["add"]
        assert (await session.execute(select(Series))).scalars().all() == []


def test_mangaupdates_links_from_mangadex():
    assert mangaupdates_id_from_link("pb8uwds") == int("pb8uwds", 36)
    assert mangaupdates_id_from_link("12345") is None  # legacy numeric id
    assert mangaupdates_id_from_link("") is None
    assert mangaupdates_id_from_link("bad/slug") is None


async def test_fetch_requires_provider_fields():
    with pytest.raises(ImportListError, match="username"):
        await fetch_entries("anilist", {})
    with pytest.raises(ImportListError, match="client ID"):
        await fetch_entries("myanimelist", {"username": "me"})
    with pytest.raises(ImportListError, match="password"):
        await fetch_entries("mangaupdates", {"username": "me"})


@respx.mock
async def test_anilist_list_skips_novels_and_explains_errors():
    respx.post(ANILIST_URL).mock(side_effect=[
        httpx.Response(200, json={"data": {"MediaListCollection": {
            "hasNextChunk": False,
            "lists": [{"entries": [
                {"media": {"id": 1, "type": "MANGA", "format": "MANGA",
                           "title": {"romaji": "Ichi", "english": "One"}}},
                {"media": {"id": 2, "type": "MANGA", "format": "NOVEL",
                           "title": {"romaji": "Ni"}}},
            ]}],
        }}}),
        httpx.Response(404, json={"errors": [{"message": "Private User", "status": 404}],
                                  "data": {"MediaListCollection": None}}),
    ])
    entries = await fetch_entries("anilist", {"username": "me"})
    assert [(e.key, e.title, e.anilist_id) for e in entries] == [("anilist:1", "One", 1)]
    with pytest.raises(ImportListError, match="Private User"):
        await fetch_entries("anilist", {"username": "hidden"})


@respx.mock
async def test_myanimelist_pages_and_maps_to_anilist():
    first = respx.get(f"{MAL_API_URL}/users/me/mangalist", params={"status": "reading"}).respond(
        json={
            "data": [
                {"node": {"id": 11, "title": "Berserk", "media_type": "manga",
                          "alternative_titles": {"en": "Berserk", "synonyms": ["Berserk Max"]},
                          "start_date": "1989-08-25"}},
                {"node": {"id": 12, "title": "Some Novel", "media_type": "light_novel"}},
            ],
            "paging": {"next": f"{MAL_API_URL}/users/me/mangalist?offset=2&page=2"},
        })
    second = respx.get(f"{MAL_API_URL}/users/me/mangalist", params={"page": "2"}).respond(
        json={"data": [{"node": {"id": 13, "title": "Unmapped"}}], "paging": {}}
    )
    respx.post(ANILIST_URL).respond(json={"data": {"Page": {
        "pageInfo": {"hasNextPage": False}, "media": [{"id": 30002, "idMal": 11}],
    }}})
    entries = await fetch_entries(
        "myanimelist", {"username": "me", "client_id": "abc", "statuses": ["reading"]}
    )
    assert first.calls[0].request.headers["X-MAL-CLIENT-ID"] == "abc"
    assert second.called
    assert [(e.key, e.anilist_id, e.year) for e in entries] == [
        ("myanimelist:11", 30002, 1989), ("myanimelist:13", None, None),
    ]


@respx.mock
async def test_mangaupdates_logs_in_reads_lists_and_logs_out():
    respx.put(f"{MU_URL}/account/login").respond(
        json={"status": "success", "context": {"session_token": "tok", "uid": 1}}
    )
    reading = respx.post(f"{MU_URL}/lists/0/search").respond(json={
        "total_hits": 2,
        "results": [
            {"record": {"series": {"id": 100, "title": "Alpha"}, "list_id": 0}},
            {"record": {"series": {"id": 200, "title": "Beta"}, "list_id": 0}},
        ],
    })
    respx.post(f"{MU_URL}/lists/1/search").respond(json={
        "total_hits": 1, "results": [{"record": {"series": {"id": 100, "title": "Alpha"}}}],
    })
    logout = respx.post(f"{MU_URL}/account/logout").respond(json={"status": "success"})
    entries = await fetch_entries(
        "mangaupdates", {"username": "me", "password": "pw", "statuses": ["0", "1"]}
    )
    assert reading.calls[0].request.headers["Authorization"] == "Bearer tok"
    assert [(e.key, e.mangaupdates_id) for e in entries] == [
        ("mangaupdates:100", 100), ("mangaupdates:200", 200),
    ]
    assert logout.called


@respx.mock
async def test_mangaupdates_bad_login():
    respx.put(f"{MU_URL}/account/login").respond(401, json={"status": "exception"})
    with pytest.raises(ImportListError, match="rejected"):
        await fetch_entries("mangaupdates", {"username": "me", "password": "bad"})
