import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.api import apikeys
from mangarr.api.deps import require_api_key
from mangarr.models import ApiKey, Base
from mangarr.schemas import ApiKeyCreateIn


@pytest.fixture
async def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'apikeys.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_list_and_delete(session):
    created = await apikeys.create_api_key(ApiKeyCreateIn(name="NextPanel"), session)
    assert created.name == "NextPanel"
    assert len(created.key) == 32  # token_hex(16)

    rows = await apikeys.list_api_keys(session)
    assert [r.key for r in rows] == [created.key]

    await apikeys.delete_api_key(created.id, session)
    assert await apikeys.list_api_keys(session) == []


@pytest.mark.asyncio
async def test_create_rejects_blank_name(session):
    with pytest.raises(HTTPException) as exc:
        await apikeys.create_api_key(ApiKeyCreateIn(name="   "), session)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_delete_missing_key_404s(session):
    with pytest.raises(HTTPException) as exc:
        await apikeys.delete_api_key(999, session)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_require_api_key_accepts_managed_key(session, monkeypatch):
    monkeypatch.setattr("mangarr.api.deps.get_api_key", lambda: "bootstrap-key")
    created = await apikeys.create_api_key(ApiKeyCreateIn(name="script"), session)
    assert created.last_used_at is None

    # bootstrap key (used by the web UI) is accepted
    await require_api_key("bootstrap-key", session)

    # a managed key is accepted and stamps last_used_at
    await require_api_key(created.key, session)
    refreshed = await session.get(ApiKey, created.id)
    assert refreshed.last_used_at is not None

    # SQLite drops timezone information from DateTime columns. A later API
    # request must still be accepted after reading that value back.
    await session.refresh(created)
    assert created.last_used_at.tzinfo is None
    await require_api_key(created.key, session)


@pytest.mark.asyncio
async def test_require_api_key_rejects_unknown_and_empty(session, monkeypatch):
    monkeypatch.setattr("mangarr.api.deps.get_api_key", lambda: "bootstrap-key")
    for bad in ("", "not-a-real-key"):
        with pytest.raises(HTTPException) as exc:
            await require_api_key(bad, session)
        assert exc.value.status_code == 401
