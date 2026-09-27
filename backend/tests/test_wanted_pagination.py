from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.api.system import wanted, wanted_page
from mangarr.models import Base, Chapter, Series


async def test_wanted_pagination_keeps_all_chapters_reachable():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        series = Series(title="Test", monitored=True)
        series.chapters = [Chapter(number=i, monitored=True) for i in range(1, 102)]
        series.chapters.append(Chapter(number=102, monitored=True, downloaded=True))
        series.chapters.append(Chapter(number=103, monitored=True, excluded=True))
        session.add(series)
        await session.commit()
        first = await wanted_page(100, 0, session)
        second = await wanted_page(100, 100, session)
        assert first["total"] == second["total"] == 101
        assert len(first["items"]) == 100
        assert [row.number for row in second["items"]] == [101]
        assert len(await wanted(session=session)) == 100  # legacy contract
    await engine.dispose()
