from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.settings import settings

engine_kwargs: dict = {"pool_pre_ping": True}
if settings.database_null_pool:
    engine_kwargs["poolclass"] = NullPool
    engine_kwargs.pop("pool_pre_ping", None)

engine = create_async_engine(settings.database_url, **engine_kwargs)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session
