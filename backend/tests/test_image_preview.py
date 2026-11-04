import asyncio
import base64
from io import BytesIO

import httpx
import pytest
from PIL import Image
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr import settings_service
from mangarr.api import deps
from mangarr.config import config
from mangarr.db import get_session
from mangarr.download import image_preview
from mangarr.download.images import ImageOptimization, optimize_page
from mangarr.main import api, app
from mangarr.models import Base


@pytest.fixture
async def client(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'preview.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        async def override():
            yield session
        api.dependency_overrides[get_session] = override
        monkeypatch.setattr(config, 'auth_username', '')
        monkeypatch.setattr(config, 'auth_password', SecretStr(''))
        monkeypatch.setattr(deps, '_api_key', 'preview-test-key')
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
                yield client, session
        finally:
            api.dependency_overrides.clear()
    await engine.dispose()


def image_bytes(payload):
    return base64.b64decode(payload['data_url'].split(',', 1)[1])


async def test_preview_requires_authentication(client):
    client, _ = client
    assert (await client.get('/api/v1/settings/image-optimization/sample')).status_code == 401
    assert (await client.post('/api/v1/settings/image-optimization/preview', json={})).status_code == 401


@pytest.mark.parametrize('policy', [ImageOptimization(), ImageOptimization(True, 'webp', 30),
                                  ImageOptimization(True, 'jpeg', 90, 600)])
async def test_preview_uses_actual_codec_without_saving_settings(client, policy):
    client, session = client
    before = await settings_service.get_all(session)
    response = await client.post('/api/v1/settings/image-optimization/preview',
        headers={'X-Api-Key': 'preview-test-key'}, json={
            'image_optimization_enabled': 'true' if policy.enabled else 'false',
            'image_optimization_format': policy.format,
            'image_optimization_quality': str(policy.quality),
            'image_optimization_max_width': str(policy.max_width),
        })
    assert response.status_code == 200
    payload = response.json()
    original = image_preview.sample_page()
    result = image_bytes(payload)
    assert result == optimize_page(original, policy)
    assert payload['size'] == len(result)
    assert payload['original_kept'] == (result == original)
    with Image.open(BytesIO(result)) as image:
        image.load()
        assert [payload['width'], payload['height']] == list(image.size)
        if policy.max_width:
            assert image.size == (600, 800)
    assert await settings_service.get_all(session) == before
    assert before['image_optimization_enabled'] == 'false'


async def test_sample_and_off_preview_preserve_original_bytes(client):
    client, _ = client
    headers = {'X-Api-Key': 'preview-test-key'}
    sample = (await client.get('/api/v1/settings/image-optimization/sample', headers=headers)).json()
    off = (await client.post('/api/v1/settings/image-optimization/preview', headers=headers, json={})).json()
    assert sample == off
    assert image_bytes(sample) == image_preview.sample_page()
    assert (sample['width'], sample['height']) == (1200, 1600)
    assert sample['data_url'].startswith('data:image/png;base64,')


@pytest.mark.parametrize('body', [
    {'image_optimization_quality': '0'}, {'image_optimization_quality': '101'},
    {'image_optimization_quality': '1.5'}, {'image_optimization_format': 'png'},
    {'image_optimization_max_width': '-1'}, {'url': 'http://example.com/image.jpg'},
    {'image_optimization_enabled': 'yes'},
])
async def test_invalid_preview_settings_do_not_start_work(client, monkeypatch, body):
    client, _ = client
    async def unexpected(*args):
        pytest.fail('codec work started for invalid input')
    monkeypatch.setattr(image_preview, 'preview_image', unexpected)
    response = await client.post('/api/v1/settings/image-optimization/preview',
        headers={'X-Api-Key': 'preview-test-key'}, json=body)
    assert response.status_code == 422


async def test_busy_preview_rejected_without_queuing_more_work(client, monkeypatch):
    client, _ = client
    entered, release = asyncio.Event(), asyncio.Event()
    real_work = image_preview.run_image_work
    async def blocked(*args):
        entered.set()
        await release.wait()
        return await real_work(*args)
    monkeypatch.setattr(image_preview, 'run_image_work', blocked)
    headers = {'X-Api-Key': 'preview-test-key'}
    first = asyncio.create_task(client.get('/api/v1/settings/image-optimization/sample', headers=headers))
    await asyncio.wait_for(entered.wait(), timeout=2)
    try:
        second = await client.post('/api/v1/settings/image-optimization/preview', headers=headers, json={})
        assert second.status_code == 429
        assert second.headers['Retry-After'] == '1'
    finally:
        release.set()
        assert (await first).status_code == 200
