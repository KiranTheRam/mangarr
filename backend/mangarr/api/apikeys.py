import secrets

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import ApiKey
from ..schemas import ApiKeyCreateIn, ApiKeyOut

router = APIRouter(prefix="/apikeys", tags=["apikeys"])


@router.get("", response_model=list[ApiKeyOut])
async def list_api_keys(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(ApiKey).order_by(ApiKey.id))).scalars().all()
    return rows


@router.post("", response_model=ApiKeyOut, status_code=201)
async def create_api_key(body: ApiKeyCreateIn, session: AsyncSession = Depends(get_session)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "API key name cannot be empty")
    key = ApiKey(name=name, key=secrets.token_hex(16))
    session.add(key)
    await session.commit()
    await session.refresh(key)
    return key


@router.delete("/{key_id}", status_code=204)
async def delete_api_key(key_id: int, session: AsyncSession = Depends(get_session)):
    row = await session.get(ApiKey, key_id)
    if row is None:
        raise HTTPException(404, "API key not found")
    await session.delete(row)
    await session.commit()
