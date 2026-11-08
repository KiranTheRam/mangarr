"""Tests for preferred title language (issue #17)."""

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from mangarr import db, settings_service
from mangarr.adding import AddOptions, create_series
from mangarr.api import series as series_api
from mangarr.jobs import tasks
from mangarr.library.naming import series_folder
from mangarr.metadata.anilist import provider as anilist_provider
from mangarr.metadata.base import SeriesMetadata
from mangarr.metadata.mangaupdates import provider as mu_provider
from mangarr.models import Base, RootFolder, Series, Setting
from mangarr.schemas import SeriesUpdateIn
from mangarr.titles import compute_title_variants, pick_title


@pytest.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


class TestTitleVariantsAndPicks:
    def test_anilist_labels_flow_into_variants_and_picks(self):
        meta = SeriesMetadata(
            provider="anilist",
            provider_id="1",
            title="Attack on Titan",
            titles={
                "english": "Attack on Titan",
                "romaji": "Shingeki no Kyojin",
                "native": "進撃の巨人",
            },
            alt_titles=["Shingeki no Kyojin", "進撃の巨人"],
        )
        variants = compute_title_variants(meta.titles, meta.title, meta.alt_titles)
        assert variants == {
            "english": "Attack on Titan",
            "romaji": "Shingeki no Kyojin",
            "native": "進撃の巨人",
        }
        assert pick_title("english", variants, meta.title) == "Attack on Titan"
        assert pick_title("romaji", variants, meta.title) == "Shingeki no Kyojin"
        assert pick_title("native", variants, meta.title) == "進撃の巨人"
        assert pick_title("provider", variants, meta.title) == "Attack on Titan"

    def test_pick_title_fallback_to_provider_title(self):
        # When preferred variant is missing, fall back to provider title
        variants = {"english": "", "romaji": "Sousou no Frieren", "native": ""}
        assert pick_title("english", variants, "Sousou no Frieren") == "Sousou no Frieren"
        assert pick_title("native", variants, "Sousou no Frieren") == "Sousou no Frieren"

    def test_mangaupdates_unlabeled_provider_variants(self):
        # English from hint, romaji = canonical Latin, native picks kana over Chinese
        canonical = "Sousou no Frieren"
        alt_titles = [
            "Chinese Title 葬送的芙莉莲",
            "Japanese Kana Title 葬送のフリーレン",
        ]
        variants = compute_title_variants(
            None,
            canonical,
            alt_titles,
            english_hint="Frieren: Beyond Journey's End",
        )
        assert variants["english"] == "Frieren: Beyond Journey's End"
        assert variants["romaji"] == "Sousou no Frieren"
        assert variants["native"] == "Japanese Kana Title 葬送のフリーレン"

    def test_mangaupdates_romaji_empty_when_canonical_is_english(self):
        variants = compute_title_variants(
            None,
            "Attack on Titan",
            ["Shingeki no Kyojin"],
            english_hint="Attack on Titan",
        )
        assert variants["english"] == "Attack on Titan"
        assert variants["romaji"] == ""


class TestCreateSeriesUnderPreferences:
    @pytest.mark.parametrize(
        ("pref", "expected_title", "expected_folder"),
        [
            ("provider", "Attack on Titan", "Attack on Titan"),
            ("english", "Attack on Titan", "Attack on Titan"),
            ("romaji", "Shingeki no Kyojin", "Shingeki no Kyojin"),
            ("native", "進撃の巨人", "進撃の巨人"),
        ],
    )
    async def test_create_series_under_preference(
        self, session, monkeypatch, pref, expected_title, expected_folder
    ):
        await session.merge(Setting(key="preferred_title_language", value=pref))
        await session.commit()

        root = RootFolder(path="/manga")
        session.add(root)
        await session.commit()

        mock_meta = SeriesMetadata(
            provider="anilist",
            provider_id="101",
            title="Attack on Titan",
            titles={
                "english": "Attack on Titan",
                "romaji": "Shingeki no Kyojin",
                "native": "進撃の巨人",
            },
            alt_titles=["Shingeki no Kyojin", "進撃の巨人"],
        )

        async def fake_get_series(provider_id: str):
            return mock_meta

        monkeypatch.setattr(anilist_provider, "get_series", fake_get_series)

        series = await create_series(
            session,
            AddOptions(root_folder_id=root.id),
            anilist_id=101,
        )

        assert series.title == expected_title
        assert series.sort_title == expected_title.lower()
        assert series.folder_name == expected_folder
        assert series.provider_title == "Attack on Titan"
        # alt_titles must contain provider title when title differs
        alt_list = series.alt_titles.split("\n")
        if series.title != series.provider_title:
            assert series.provider_title in alt_list
        assert series.title not in alt_list


class TestRefreshMetadata:
    async def test_refresh_unlocked_series_follows_preference_and_keeps_english_hint(
        self, session, monkeypatch
    ):
        series = Series(
            id=1,
            mangaupdates_id=1001,
            title="Sousou no Frieren",
            provider_title="Sousou no Frieren",
            title_english="Frieren: Beyond Journey's End",
            title_romaji="Sousou no Frieren",
            title_native="葬送のフリーレン",
            title_locked=False,
            folder_name="Sousou no Frieren",
            alt_titles="Frieren: Beyond Journey's End\n葬送のフリーレン",
        )
        session.add(series)
        # Change preference to english
        await session.merge(Setting(key="preferred_title_language", value="english"))
        await session.commit()

        mock_meta = SeriesMetadata(
            provider="mangaupdates",
            provider_id="1001",
            title="Sousou no Frieren",
            alt_titles=["Frieren: Beyond Journey's End", "葬送のフリーレン"],
        )

        async def fake_mu_get(provider_id: str):
            return mock_meta

        monkeypatch.setattr(mu_provider, "get_series", fake_mu_get)

        await tasks.refresh_series_metadata(session, series)

        assert series.title == "Frieren: Beyond Journey's End"
        assert series.sort_title == "frieren: beyond journey's end"
        assert series.title_english == "Frieren: Beyond Journey's End"
        assert "Sousou no Frieren" in series.alt_titles.split("\n")

    async def test_refresh_locked_series_keeps_title(self, session, monkeypatch):
        series = Series(
            id=2,
            anilist_id=2002,
            title="My Custom Title",
            provider_title="Old Provider Title",
            title_english="English Name",
            title_romaji="Romaji Name",
            title_native="Native Name",
            title_locked=True,
            folder_name="My Custom Title",
            alt_titles="Old Provider Title",
        )
        session.add(series)
        await session.merge(Setting(key="preferred_title_language", value="english"))
        await session.commit()

        mock_meta = SeriesMetadata(
            provider="anilist",
            provider_id="2002",
            title="New Provider Title",
            titles={
                "english": "New English Name",
                "romaji": "New Romaji Name",
                "native": "New Native Name",
            },
            alt_titles=["New English Name", "New Romaji Name"],
        )

        async def fake_ani_get(provider_id: str):
            return mock_meta

        monkeypatch.setattr(anilist_provider, "get_series", fake_ani_get)

        await tasks.refresh_series_metadata(session, series)

        # Title remains locked
        assert series.title == "My Custom Title"
        assert series.provider_title == "New Provider Title"
        assert series.title_english == "New English Name"
        assert "New Provider Title" in series.alt_titles.split("\n")


class TestDataSafetyRetitleFolder:
    async def test_retitle_with_empty_folder_name_pins_old_folder(self, session):
        series = Series(
            id=10,
            title="Old Title",
            provider_title="Old Title",
            folder_name="",
            title_locked=False,
        )
        session.add(series)
        await session.commit()

        # Update title via series_api.update_series
        updated = await series_api.update_series(
            10,
            SeriesUpdateIn(title="New Title"),
            session=session,
        )
        assert updated.title == "New Title"
        assert updated.folder_name == series_folder("Old Title")
        assert updated.title_locked is True


class TestSeriesApiTitleLocking:
    async def test_put_title_locks(self, session):
        series = Series(
            id=20,
            title="Initial Title",
            provider_title="Initial Title",
            folder_name="Initial Title",
            title_locked=False,
        )
        session.add(series)
        await session.commit()

        res = await series_api.update_series(
            20,
            SeriesUpdateIn(title="Custom Title"),
            session=session,
        )
        assert res.title == "Custom Title"
        assert res.title_locked is True

    async def test_put_title_locked_false_unlocks_and_repicks(self, session):
        await session.merge(Setting(key="preferred_title_language", value="english"))
        series = Series(
            id=21,
            title="Custom Locked Title",
            provider_title="Provider Japanese Title",
            title_english="Official English Title",
            title_romaji="Provider Japanese Title",
            title_native="日本語",
            folder_name="Custom Locked Title",
            title_locked=True,
        )
        session.add(series)
        await session.commit()

        res = await series_api.update_series(
            21,
            SeriesUpdateIn(title_locked=False),
            session=session,
        )
        assert res.title_locked is False
        assert res.title == "Official English Title"

    async def test_put_empty_title_rejected(self, session):
        series = Series(
            id=22,
            title="Some Title",
            folder_name="Some Title",
            title_locked=False,
        )
        session.add(series)
        await session.commit()

        with pytest.raises(HTTPException) as exc:
            await series_api.update_series(
                22,
                SeriesUpdateIn(title="   "),
                session=session,
            )
        assert exc.value.status_code == 422


class TestApplyTitlePreferenceEndpoint:
    async def test_apply_title_preference_changes_unlocked_series_only(self, session):
        await session.merge(Setting(key="preferred_title_language", value="romaji"))

        # Series 1: Unlocked, should change to Romaji
        s1 = Series(
            id=31,
            title="English One",
            provider_title="English One",
            title_english="English One",
            title_romaji="Romaji One",
            title_native="Native One",
            folder_name="English One",
            title_locked=False,
        )
        # Series 2: Locked, should NOT change
        s2 = Series(
            id=32,
            title="Custom Two",
            provider_title="Provider Two",
            title_english="English Two",
            title_romaji="Romaji Two",
            title_native="Native Two",
            folder_name="Custom Two",
            title_locked=True,
        )
        # Series 3: Unlocked, already Romaji, should count as 0 changes
        s3 = Series(
            id=33,
            title="Romaji Three",
            provider_title="Romaji Three",
            title_english="English Three",
            title_romaji="Romaji Three",
            title_native="Native Three",
            folder_name="Romaji Three",
            title_locked=False,
        )
        session.add_all([s1, s2, s3])
        await session.commit()

        result = await series_api.apply_title_preference(session=session)
        assert result.count == 1

        await session.refresh(s1)
        await session.refresh(s2)
        await session.refresh(s3)

        assert s1.title == "Romaji One"
        assert s2.title == "Custom Two"
        assert s3.title == "Romaji Three"


class TestDatabaseMigration:
    async def test_existing_db_migration_backfills_provider_title(self, tmp_path, monkeypatch):
        # Create a database where series table exists without the new columns
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test_migration.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for col in [
                "provider_title",
                "title_english",
                "title_romaji",
                "title_native",
                "title_locked",
            ]:
                await conn.exec_driver_sql(f"ALTER TABLE series DROP COLUMN {col}")
            # Insert a series from the past
            await conn.exec_driver_sql(
                "INSERT INTO series (id, title, sort_title, folder_name, alt_titles, description, status, monitored, folder_pinned, added_at, search_title, search_templates, monitor_mode, source_priority, blocked_sources, preferred_groups, blocked_groups, upgrades_enabled, upgrade_cutoff, merge_volumes, genres, cover_url, banner_url) "
                "VALUES (1, 'Berserk', 'berserk', 'Berserk', '', '', 'unknown', 1, 0, '2026-01-01', '', '', 'all', '', '', '', '', 0, '', 0, '', '', '')"
            )

        monkeypatch.setattr(db, "engine", engine)
        monkeypatch.setattr(db.config, "data_dir", tmp_path)

        # Run init_db()
        await db.init_db()

        async with engine.connect() as conn:
            info = await conn.exec_driver_sql("PRAGMA table_info(series)")
            columns = {row[1]: row for row in info.fetchall()}
            row = (await conn.exec_driver_sql("SELECT provider_title, title_locked FROM series WHERE id = 1")).fetchone()

        await engine.dispose()

        assert "provider_title" in columns
        assert "title_english" in columns
        assert "title_romaji" in columns
        assert "title_native" in columns
        assert "title_locked" in columns

        assert row[0] == "Berserk"
        assert row[1] == 0


class TestMetadataResultsPreferredTitle:
    async def test_search_metadata_populates_preferred_title(self, session, monkeypatch):
        await session.merge(Setting(key="preferred_title_language", value="romaji"))
        await session.commit()

        from mangarr.api.search import metadata_results

        mock_meta = SeriesMetadata(
            provider="anilist",
            provider_id="999",
            title="Attack on Titan",
            titles={
                "english": "Attack on Titan",
                "romaji": "Shingeki no Kyojin",
                "native": "進撃の巨人",
            },
            alt_titles=["Shingeki no Kyojin", "進撃の巨人"],
        )

        async def fake_search(query: str, limit: int = 20):
            return [mock_meta]

        monkeypatch.setattr(anilist_provider, "search", fake_search)

        results = await metadata_results(session, "anilist", "attack")
        assert len(results) == 1
        assert results[0].preferred_title == "Shingeki no Kyojin"
        assert results[0].english_title == "Attack on Titan"
