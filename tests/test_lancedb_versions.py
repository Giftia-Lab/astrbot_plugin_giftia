from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import httpx
import pytest
import pytest_asyncio
from core.database.database import Database
from core.database.schema import initialize_database
from core.memory import lancedb_versions
from core.web import lancedb_api
from core.web.webui_manager import WebUIManager
from fastapi import FastAPI, Request
from packaging.tags import Tag
from packaging.version import Version

from astrbot.api.web import PluginRequest, bind_request_context


@pytest_asyncio.fixture
async def versions(monkeypatch):
    monkeypatch.setattr(lancedb_versions, "DEMO_MODE", False)
    monkeypatch.setattr(lancedb_versions, "is_packaged_desktop_runtime", lambda: False)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await initialize_database(conn)
        yield lancedb_versions.LanceDBVersions(Database(conn), MagicMock())


@pytest.mark.asyncio
async def test_index_limits_results_to_supported_stable_compatible_wheels(
    versions, monkeypatch
):
    monkeypatch.setattr(lancedb_versions.platform, "python_version", lambda: "3.12.0")
    monkeypatch.setattr(
        lancedb_versions,
        "sys_tags",
        lambda: [Tag("cp312", "abi3", "macosx_11_0_arm64")],
    )
    response = versions.session.get.return_value.__aenter__.return_value
    response.raise_for_status = MagicMock()
    response.json = AsyncMock(
        return_value={
            "files": [
                {
                    "filename": f"lancedb-{version}-cp312-abi3-macosx_11_0_arm64.whl",
                    **fields,
                }
                for version, fields in [
                    ("0.30.0", {}),
                    ("0.33.0", {"requires-python": ">=3.10"}),
                    ("0.33.0", {}),
                    ("1.0.0", {}),
                    ("0.29.9", {}),
                    ("0.34.0rc1", {}),
                    ("0.35.0", {"yanked": "Withdrawn"}),
                    ("0.36.0", {"requires-python": ">=3.13"}),
                ]
            ]
            + [
                {"filename": "lancedb-0.37.0-cp312-abi3-win_amd64.whl"},
                {"filename": "lancedb-0.38.0.tar.gz"},
            ]
        }
    )
    assert await versions.available_versions(refresh=True) == ["0.33.0", "0.30.0"]
    assert await versions.available_versions() == ["0.33.0", "0.30.0"]
    versions.session.get.assert_called_once()


@pytest.mark.asyncio
async def test_schedule_applies_once_in_a_fresh_process(versions, monkeypatch):
    modules = {"lancedb": object()}
    monkeypatch.setattr(lancedb_versions, "sys", SimpleNamespace(modules=modules))
    installed = "0.33.0"
    monkeypatch.setattr(
        lancedb_versions, "installed_lancedb_version", lambda: installed
    )

    async def install(version):
        nonlocal installed
        installed = str(version)

    installer = AsyncMock(side_effect=install)
    monkeypatch.setattr(versions, "_install", installer)
    await versions.save_plan("0.30.0")
    await versions.apply_pending()
    assert versions.restart_deferred
    assert await versions.pending() == {"version": "0.30.0"}
    installer.assert_not_awaited()

    modules.clear()
    await versions.apply_pending()

    modules["lancedb"] = object()
    await versions.save_plan("0.30.0")
    await versions.apply_pending()
    installer.assert_awaited_once_with(Version("0.30.0"))
    assert installed == "0.30.0"
    assert not versions.restart_deferred
    assert await versions.pending() == {}


@pytest.mark.asyncio
async def test_failed_install_keeps_the_failure_visible_without_retrying_on_boot(
    versions, monkeypatch
):
    monkeypatch.setattr(lancedb_versions, "sys", SimpleNamespace(modules={}))
    monkeypatch.setattr(lancedb_versions, "installed_lancedb_version", lambda: "0.33.0")
    installer = AsyncMock(side_effect=RuntimeError("Index unreachable"))
    monkeypatch.setattr(versions, "_install", installer)
    await versions.save_plan("0.30.0")
    await versions.apply_pending()
    await versions.apply_pending()
    installer.assert_awaited_once()
    saved = await versions.pending()
    assert saved["version"] == "0.30.0" and "安装失败" in saved["error"]


@pytest.mark.asyncio
async def test_registered_dashboard_routes_validate_schedule_and_cancel(
    versions, monkeypatch
):
    monkeypatch.setattr(lancedb_api, "installed_lancedb_version", lambda: "0.33.0")
    monkeypatch.setattr(
        versions, "available_versions", AsyncMock(return_value=["0.33.0", "0.30.0"])
    )
    plugin = SimpleNamespace(
        context=MagicMock(),
        ltm=SimpleNamespace(lancedb_version="0.32.0"),
        lancedb_versions=versions,
    )
    WebUIManager(plugin).register_routes()
    routes = {
        (entry.kwargs["route"], method): entry.kwargs["view_handler"]
        for entry in plugin.context.register_web_api.call_args_list
        for method in entry.kwargs["methods"]
    }
    app = FastAPI()

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def dispatch(request: Request):
        with bind_request_context(PluginRequest(request)):
            return await routes[(request.url.path, request.method)]()

    endpoint = "/astrbot_plugin_giftia/dependencies/lancedb"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        initial = (await client.get(endpoint)).json()["data"]
        assert initial["running_version"] == "0.32.0"
        assert initial["installed_version"] == "0.33.0"
        assert (await client.get(f"{endpoint}/versions")).json()["data"] == [
            "0.33.0",
            "0.30.0",
        ]
        for invalid in ("0.29.0", "0.30.0 --no-deps", "0.33.0", {}, False):
            assert (
                await client.post(endpoint, json={"version": invalid})
            ).status_code == 400
        assert (await client.post(endpoint, json={})).status_code == 400
        assert (
            await client.post(endpoint, content='{"version":"0.30.0"}')
        ).status_code == 400
        assert initial["plan"] == await versions.pending() == {}

        planned = (await client.post(endpoint, json={"version": "0.30.0"})).json()[
            "data"
        ]
        assert planned["plan"] == {"version": "0.30.0"}
        assert planned["installed_version"] == "0.33.0"
        cancelled = (await client.post(endpoint, json={"version": None})).json()["data"]
        assert cancelled["plan"] == {}

        versions.switch_unavailable = "Demo mode"
        assert (
            await client.post(endpoint, json={"version": "0.30.0"})
        ).status_code == 403
        assert await versions.pending() == {}
