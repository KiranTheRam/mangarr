import asyncio
import logging
import stat
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr import kavita, notifications, settings_service
from mangarr.api import deps
from mangarr.browser_setup import LOGIN_FILE
from mangarr.config import AppConfig, config
from mangarr.db import get_session
from mangarr.main import api, app
from mangarr.models import ApiKey, Base


@pytest.fixture
async def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'security.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def client(session, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "data_dir", tmp_path)
    monkeypatch.setattr(config, "auth_username", "")
    monkeypatch.setattr(config, "auth_password", SecretStr(""))
    monkeypatch.setattr(config, "allow_unauthenticated_browser", False)
    monkeypatch.setattr(config, "allowed_hosts", ["reader.example"])
    monkeypatch.setattr(deps, "_api_key", "bootstrap")

    async def override():
        yield session

    api.dependency_overrides[get_session] = override
    with_session = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")
    async with with_session as client:
        yield client
    api.dependency_overrides.clear()


def enable_login(monkeypatch):
    monkeypatch.setattr(config, "auth_username", "reader")
    monkeypatch.setattr(config, "auth_password", SecretStr("password"))


async def test_browser_bootstrap_and_independent_api_auth(client, session, monkeypatch):
    response = await client.get("/initialize.json", headers={"X-Forwarded-For": "127.0.0.1"})
    assert response.status_code == 403
    assert "bootstrap" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert (await client.get("/api/v1/settings", headers={"X-Api-Key": "bootstrap"})).status_code == 200
    monkeypatch.setattr(config, "allow_unauthenticated_browser", True)
    assert (await client.get("/initialize.json")).json()["apiKey"] == "bootstrap"
    enable_login(monkeypatch)
    for path in ("/", "/initialize.json", "/docs", "/openapi.json", "/assets/missing", "/api/v1-other"):
        response = await client.get(path, headers={"X-Api-Key": "bootstrap", "X-Forwarded-User": "reader"})
        assert response.status_code == 401
        assert "bootstrap" not in response.text
        assert response.headers["www-authenticate"].startswith("Basic ")
    assert (await client.get("/ping")).status_code == 200
    for auth in (httpx.BasicAuth("reader", "wrong"), httpx.BasicAuth("wrong", "password")):
        assert (await client.get("/initialize.json", auth=auth)).status_code == 401
    for value in ("Bearer bogus", "Basic !!!", "Basic Og=="):
        assert (await client.get("/initialize.json", headers={"Authorization": value})).status_code == 401
    response = await client.get("/initialize.json", auth=httpx.BasicAuth("reader", "password"))
    assert response.json()["apiKey"] == "bootstrap"
    assert response.headers["cache-control"] == "no-store"
    assert (await client.get("/api/v1/settings", auth=httpx.BasicAuth("reader", "password"))).status_code == 401
    assert (await client.get("/api/v1/settings", headers={"X-Api-Key": "wrong"})).status_code == 401
    session.add(ApiKey(name="integration", key="managed"))
    await session.commit()
    for key in ("managed", "bootstrap"):
        assert (await client.get("/api/v1/settings", headers={"X-Api-Key": key})).status_code == 200


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST:6996", "192.0.2.1:6996", "[::1]:6996", "reader.example:443"])
async def test_allowed_hosts(client, host):
    assert (await client.get("/ping", headers={"Host": host})).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "localhost.evil.example", "reader.example.evil", "localhost@evil.example", "localhost/path", "localhost#evil", "localhost:99999", "localhost:abc", "localhost\\evil", "", "localhost.", " localhost"])
async def test_rebinding_and_malformed_hosts_rejected_everywhere(client, host):
    for path in ("/initialize.json", "/api/v1/settings", "/assets/missing", "/ping"):
        response = await client.get(path, headers={"Host": host, "X-Forwarded-Host": "localhost", "X-Api-Key": "bootstrap"})
        assert response.status_code == 400
        assert "bootstrap" not in response.text


async def test_duplicate_and_missing_host(client):
    assert (await client.get("/ping", headers=[("Host", "localhost"), ("Host", "evil.example")])).status_code == 400
    request = client.build_request("GET", "/ping")
    del request.headers["host"]
    assert (await client.send(request)).status_code == 400


async def test_mount_path_cannot_bypass_browser_login(client, monkeypatch):
    transport = httpx.ASGITransport(app=app, root_path="/api/v1")
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as mounted:
        assert (await mounted.get("/api/v1/ping")).status_code == 200
        assert (await mounted.get("/api/v1/")).headers["location"] == "/api/v1/setup"
        assert (await mounted.get("/api/v1/initialize.json")).status_code == 403
        enable_login(monkeypatch)
        assert (await mounted.get("/api/v1/initialize.json")).status_code == 401
        response = await mounted.get("/api/v1/initialize.json", auth=("reader", "password"))
        assert response.json()["apiKey"] == "bootstrap"
        assert response.headers["cache-control"] == "no-store"
        assert (await mounted.get("/api/v1/api/v1/settings", headers={"X-Api-Key": "bootstrap"})).status_code == 200


async def test_browser_setup_keeps_existing_api_and_accepts_one_login(client, tmp_path, monkeypatch):
    page = await client.get("/", follow_redirects=True)
    assert page.url.path == "/setup"
    assert "Set up your Mangarr login" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert 'name="code"' not in page.text
    assert (await client.get("/initialize.json")).status_code == 403
    assert (await client.get("/api/v1/settings", headers={"X-Api-Key": "bootstrap"})).status_code == 200
    password = "correct horse battery staple"
    data = {"username": "reader", "password": password, "confirmation": password}
    headers = {"Origin": "http://localhost"}
    assert (await client.post("/setup", data=data)).status_code == 403
    assert (await client.post("/setup", data=data, headers={"Origin": "https://other.example"})).status_code == 403
    assert (await client.post("/setup", data=data, headers={**headers, "Sec-Fetch-Site": "cross-site"})).status_code == 403
    login_file = tmp_path / LOGIN_FILE
    assert not login_file.exists()
    assert (await client.post("/setup", data={**data, "confirmation": "mismatch"}, headers=headers)).status_code == 400
    assert (await client.post("/setup", data={**data, "password": "x" * 4096}, headers=headers)).status_code == 413
    responses = await asyncio.gather(
        client.post("/setup", data=data, headers=headers),
        client.post("/setup", data={**data, "username": "other"}, headers=headers),
    )
    assert any(r.status_code == 303 and r.headers["location"] == "/" for r in responses)
    assert stat.S_IMODE(login_file.stat().st_mode) == 0o600
    assert password not in login_file.read_text()

    monkeypatch.setattr(config, "allow_unauthenticated_browser", True)
    assert (await client.get("/initialize.json")).status_code == 401
    assert (await client.get("/initialize.json", auth=("reader", "wrong"))).status_code == 401
    logins = [await client.get("/initialize.json", auth=(name, password)) for name in ("reader", "other")]
    assert sorted(r.status_code for r in logins) == [200, 401]
    response = next(r for r in logins if r.status_code == 200)
    assert response.json()["apiKey"] == "bootstrap" and response.headers["cache-control"] == "no-store"
    assert (await client.post("/setup", data={**data, "username": "attacker"}, headers=headers)).status_code == 303
    assert (await client.get("/initialize.json", auth=("attacker", password))).status_code == 401


def test_partial_auth_configuration_fails_without_printing_password():
    for values in ({"auth_username": "reader"}, {"auth_password": "sensitive"}):
        with pytest.raises(ValidationError):
            AppConfig(_env_file=None, **values)
    assert "sensitive" not in repr(AppConfig(_env_file=None, auth_username="reader", auth_password="sensitive"))
    for hosts in (["*"], ["https://host"], ["host:6996"]):
        with pytest.raises(ValidationError):
            AppConfig(_env_file=None, allowed_hosts=hosts)


CONNECTIONS = [
    ("webhook", "webhook_url", "webhook_secret", "secret", "mangarr.api.settings.notifications.send_webhook", True),
    ("kavita", "kavita_url", "kavita_api_key", "api_key", "mangarr.api.settings.kavita.test_connection", ("1", [])),
    ("qbittorrent", "qbittorrent_url", "qbittorrent_password", "password", "mangarr.api.settings.test_connection", "5"),
]


@pytest.mark.parametrize("name,url_key,secret_key,field,target,result", CONNECTIONS)
async def test_secret_reuse_bound_to_destination(client, session, monkeypatch, name, url_key, secret_key, field, target, result):
    url = "https://service.example/base"
    await settings_service.set_many(session, {url_key: url, secret_key: "stored-secret", "qbittorrent_username": "user"})
    call = AsyncMock(return_value=result)
    monkeypatch.setattr(target, call)
    body = {"url": url, field: "••••••••", "username": "user"}
    headers = {"X-Api-Key": "bootstrap"}
    response = await client.post(f"/api/v1/settings/{name}/test", json=body, headers=headers)
    assert response.status_code == 200
    assert "stored-secret" in call.call_args.args
    call.reset_mock()
    for changed in (url + "/other", url.replace("https:", "http:"), "https://other.example/base", url + "/"):
        body["url"] = changed
        response = await client.post(f"/api/v1/settings/{name}/test", json=body, headers=headers)
        assert response.status_code == 422
        assert "stored-secret" not in response.text
        call.assert_not_called()
        for placeholder in ("••••••••", None):
            payload = {url_key: changed}
            if placeholder is not None:
                payload[secret_key] = placeholder
            assert (await client.put("/api/v1/settings", json=payload, headers=headers)).status_code == 422
        assert await settings_service.get(session, url_key) == url
    body.update(url=url, username="other")
    if name == "qbittorrent":
        assert (await client.post(f"/api/v1/settings/{name}/test", json=body, headers=headers)).status_code == 422
        assert (await client.put("/api/v1/settings", json={"qbittorrent_username": "other"}, headers=headers)).status_code == 422
    body.update(url="https://new.example", **{field: "replacement"})
    assert (await client.post(f"/api/v1/settings/{name}/test", json=body, headers=headers)).status_code == 200
    assert "replacement" in call.call_args.args
    assert (await client.put("/api/v1/settings", json={url_key: body["url"], secret_key: "replacement"}, headers=headers)).status_code == 200
    assert await settings_service.get(session, secret_key) == "replacement"


async def test_kavita_omitted_and_empty_key_only_reused_for_saved_url(client, session, monkeypatch):
    await settings_service.set_many(session, {"kavita_url": "https://saved.example", "kavita_api_key": "stored"})
    call = AsyncMock(return_value=("1", []))
    monkeypatch.setattr(kavita, "test_connection", call)
    for secret in ({}, {"api_key": ""}):
        assert (await client.post("/api/v1/settings/kavita/test", json={"url": "https://saved.example", **secret}, headers={"X-Api-Key": "bootstrap"})).status_code == 200
        call.assert_awaited_with("https://saved.example", "stored")
        call.reset_mock()
        assert (await client.post("/api/v1/settings/kavita/test", json={"url": "https://new.example", **secret}, headers={"X-Api-Key": "bootstrap"})).status_code == 422
        call.assert_not_called()


@pytest.mark.parametrize("name,url_key,secret_key,field,target,result", CONNECTIONS[:1] + CONNECTIONS[2:])
async def test_explicit_empty_secret_is_not_reused(client, session, monkeypatch, name, url_key, secret_key, field, target, result):
    await settings_service.set_many(session, {url_key: "https://old.example", secret_key: "stored"})
    call = AsyncMock(return_value=result)
    monkeypatch.setattr(target, call)
    assert (await client.post(f"/api/v1/settings/{name}/test", json={"url": "https://new.example", "username": "user", field: ""}, headers={"X-Api-Key": "bootstrap"})).status_code == 200
    assert "stored" not in call.call_args.args
    assert (await client.put("/api/v1/settings", json={url_key: "https://new.example", secret_key: ""}, headers={"X-Api-Key": "bootstrap"})).status_code == 200
    assert await settings_service.get(session, secret_key) == ""


async def test_integration_logs_and_errors_hide_credentials(respx_mock, caplog):
    # Leave transport logger levels as configured by main; capturing root DEBUG
    # must not enable httpx INFO or httpcore DEBUG credential records.
    caplog.set_level(logging.DEBUG)
    url = "https://kavita.example/api/Plugin/authenticate"
    auth = respx_mock.post(url).respond(200, json={"token": "private-token", "kavitaVersion": "1"})
    client = kavita.KavitaClient("https://kavita.example", "private-key")
    await client._authenticate()
    assert auth.calls.last.request.url.params["apiKey"] == "private-key"
    assert "private-key" not in caplog.text
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING
    auth.respond(500, text="private-key private-token")
    with pytest.raises(kavita.KavitaError) as error:
        await client._authenticate()
    assert "private" not in str(error.value)
    auth.mock(side_effect=httpx.ConnectError("private-key https://secret.example/?token=private-token"))
    with pytest.raises(kavita.KavitaError) as error:
        await client._authenticate()
    assert "private" not in str(error.value)
    assert error.value.__suppress_context__
    await client.close()
    secret_url = "https://webhook.example/?token=private-token"
    respx_mock.post(secret_url).respond(500, text="private-key")
    assert not await notifications.send_webhook(secret_url, "private-key", {})
    respx_mock.post(secret_url).mock(side_effect=httpx.ConnectError("private-key private-token"))
    assert not await notifications.send_webhook(secret_url, "private-key", {})
    assert "private-key" not in caplog.text
    assert "private-token" not in caplog.text


async def test_qbt_errors_do_not_echo_remote_content_or_urls(respx_mock):
    from mangarr.download.qbittorrent import QbtClient, QbtError

    client = QbtClient("https://qbt.example", "user", "private-password")
    login = respx_mock.post("https://qbt.example/api/v2/auth/login").respond(500, text="private-password")
    with pytest.raises(QbtError) as error:
        await client.version()
    assert "private-password" not in str(error.value)
    login.respond(204)
    respx_mock.get("https://qbt.example/api/v2/app/version").mock(side_effect=httpx.ConnectError("private-password"))
    with pytest.raises(QbtError) as error:
        await client.version()
    assert str(error.value) == "Cannot reach qBittorrent"
    assert error.value.__suppress_context__
    await client.close()


@pytest.mark.parametrize("path", ["/initial%69ze.json", "/initialize.json?x=1", "/api/v1/../initialize.json", "/api/v1%2f..%2f..%2finitialize.json"])
async def test_encoded_browser_paths_do_not_bypass_login(client, monkeypatch, path):
    enable_login(monkeypatch)
    response = await client.get(path, headers={"X-Api-Key": "bootstrap"}, follow_redirects=True)
    assert "bootstrap" not in response.text


async def test_unchanged_masked_save_keeps_secrets_private(client, session):
    await settings_service.set_many(session, {"kavita_url": "https://saved.example", "kavita_api_key": "stored"})
    headers = {"X-Api-Key": "bootstrap"}
    settings = (await client.get("/api/v1/settings", headers=headers)).json()
    assert settings["kavita_api_key"] == "••••••••"
    response = await client.put("/api/v1/settings", json=settings, headers=headers)
    assert response.status_code == 200
    assert response.json()["kavita_api_key"] == "••••••••"
    assert await settings_service.get(session, "kavita_api_key") == "stored"


async def test_qbt_redirect_remains_a_failed_request(respx_mock):
    from mangarr.download.qbittorrent import QbtClient, QbtError

    respx_mock.post("https://qbt.example/api/v2/auth/login").respond(204)
    respx_mock.get("https://qbt.example/api/v2/app/version").respond(302, headers={"Location": "https://elsewhere.example"})
    respx_mock.post("https://qbt.example/api/v2/torrents/add").respond(302)
    client = QbtClient("https://qbt.example", "user", "password")
    for operation in (client.version(), client.add_magnet("magnet:?xt=test", "manga")):
        with pytest.raises(QbtError, match="HTTP 302"):
            await operation
    await client.close()
