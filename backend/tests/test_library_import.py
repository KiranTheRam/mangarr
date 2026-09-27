"""Bulk import of an existing library: folder discovery, auto-matching, and
adding the confirmed folders as series."""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from mangarr import adding
from mangarr.api import library_import
from mangarr.api.library_import import best_match, folder_query, import_folders, import_library
from mangarr.metadata.base import SeriesMetadata
from mangarr.models import Base, RootFolder, Series, SeriesFolder
from mangarr.schemas import LibraryImportIn, LibraryImportItemIn, MetadataResult


@pytest.fixture
async def maker():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def refreshes(monkeypatch):
    started: list[tuple[list[int], bool]] = []
    monkeypatch.setattr(library_import, "start_refreshes",
                        lambda ids, grab_missing=False: started.append((ids, grab_missing)))
    return started


class FakeProvider:
    name = "mangaupdates"

    def __init__(self, known: dict[str, str]):
        self.known = known

    async def get_series(self, provider_id):
        title = self.known.get(provider_id)
        if title is None:
            return None
        return SeriesMetadata(provider=self.name, provider_id=provider_id, title=title)


def _result(title, alts=(), english=""):
    return MetadataResult(
        provider="mangaupdates", provider_id="1", title=title, english_title=english,
        alt_titles=list(alts), description="", status="unknown", year=None,
        cover_url="", genres=[], total_chapters=None, total_volumes=None,
    )


def test_folder_query_strips_release_decorations():
    assert folder_query("Kagurabachi (2023) [Digital]") == "Kagurabachi"
    assert folder_query("One_Piece") == "One Piece"
    assert folder_query("[Group] Berserk - ") == "Berserk"
    assert folder_query("(2019)") == "(2019)"  # nothing left: keep the name


def test_best_match_needs_a_clear_title_match():
    candidates = [_result("Something Else"), _result("Shingeki no Kyojin", ["Attack on Titan"])]
    assert best_match("Attack on Titan", candidates) == 1
    # spacing variant of the top result
    assert best_match("Kagura Bachi", [_result("Kagurabachi")]) == 0
    # a mere prefix is a different series
    assert best_match("Berserk", [_result("Berserk of Gluttony")]) is None
    assert best_match("", candidates) is None


async def test_folders_skip_those_a_series_already_uses(tmp_path: Path, maker):
    for name in ("Owned", "Extra Home", "Container", "Loose", ".hidden"):
        (tmp_path / name).mkdir()
    (tmp_path / "Container" / "Nested Series").mkdir()
    (tmp_path / "Loose" / "Loose 001.cbz").write_bytes(b"")
    (tmp_path / "not-a-folder.cbz").write_bytes(b"")
    async with maker() as session:
        root = RootFolder(path=str(tmp_path))
        session.add(root)
        await session.flush()
        a = Series(title="Owned", folder_name="Owned", root_folder_id=root.id)
        a.extra_folders.append(SeriesFolder(path="Extra Home"))
        b = Series(title="Nested", folder_name="Container/Nested Series", root_folder_id=root.id)
        session.add_all([a, b])
        await session.commit()
        folders = await import_folders(root.id, session)
    assert [(f.name, f.file_count, f.query) for f in folders] == [("Loose", 1, "Loose")]


async def test_import_adds_series_with_their_folders_pinned(tmp_path: Path, maker, monkeypatch,
                                                           refreshes):
    monkeypatch.setattr(adding, "mangaupdates", FakeProvider({"10": "Kagurabachi", "20": "Berserk"}))
    async with maker() as session:
        root = RootFolder(path=str(tmp_path))
        session.add(root)
        await session.commit()
        body = LibraryImportIn(
            root_folder_id=root.id, monitored=False, search_now=True,
            items=[
                LibraryImportItemIn(folder_name="Kagurabachi (2023)", provider="mangaupdates",
                                    provider_id=10, english_title="Kagura Bachi"),
                LibraryImportItemIn(folder_name="dup", provider="mangaupdates", provider_id=10),
                LibraryImportItemIn(folder_name="gone", provider="mangaupdates", provider_id=99),
                LibraryImportItemIn(folder_name="Berserk", provider="mangaupdates", provider_id=20),
            ],
        )
        results = await import_library(body, session)
    assert [(r.folder_name, r.status) for r in results] == [
        ("Kagurabachi (2023)", "added"), ("dup", "exists"), ("gone", "failed"), ("Berserk", "added"),
    ]
    assert results[1].series_id == results[0].series_id
    assert refreshes == [([results[0].series_id, results[3].series_id], True)]
    async with maker() as session:
        series = await session.get(Series, results[0].series_id)
        assert series.folder_name == "Kagurabachi (2023)"
        assert series.folder_pinned is True
        assert series.monitored is False
        assert "Kagura Bachi" in series.alt_titles


async def test_library_index_matches_by_id_then_title(maker):
    async with maker() as session:
        session.add_all([
            Series(title="Shingeki no Kyojin", alt_titles="Attack on Titan", mangaupdates_id=5),
            Series(title="Berserk", anilist_id=30002),
        ])
        await session.commit()
        index = await adding.LibraryIndex.load(session)
    assert index.find(anilist_id=30002) is not None
    assert index.find(mangaupdates_id=5) == index.find(titles=["Attack on Titan!"])
    assert index.find(anilist_id=1, titles=["Unrelated"]) is None
