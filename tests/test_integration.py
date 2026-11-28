"""Guard saved-account identity and the public plugin editing contract."""

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from app.config import ConfigCollection, config_manager
from app.core import Config, Plugin
from app.core.config.upgrade import convert_legacy
from app.models.config import PluginRecord
from app.plugin.calls import CallGate
from app.plugin.openapi import bind_openapi
from app.plugin.types import PluginState
from app.utils import io
from fastapi import FastAPI

from whimbox_adapter.config import WhimboxConfig
from whimbox_adapter.plugin import Plugin as Adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_children", [False, True])
async def test_legacy_accounts_survive_save_and_reload(tmp_path, legacy_children):
    sid, first, second = [str(uuid4()) for _ in range(3)]
    payload = {
        "order": [{"uid": sid, "type": "WhimboxConfig"}],
        "data": {
            sid: {
                "Info": {"Name": "原托管"},
                "UserData": {
                    "order": [
                        {"uid": uid, "type": "WhimboxUserConfig"}
                        for uid in (first, second)
                    ],
                    "data": {
                        uid: {
                            "Info": {"Name": uid, "Status": True},
                            "Task": {"Tasks": '{"daily":true}'},
                        }
                        for uid in (first, second)
                    },
                },
            }
        },
    }
    if legacy_children:
        payload["data"][sid]["Run"] = {"ExecutionSession": "desktop"}
        script = payload["data"][sid]
        users = script.pop("UserData")
        script["SubConfigsInfo"] = {
            "UserData": {"instances": users["order"], **users["data"]}
        }
    scripts = ConfigCollection.build([WhimboxConfig])
    if legacy_children:
        # Startup converts pre-YAML documents before activating plugin types.
        payload = convert_legacy(
            {"instances": payload["order"], **payload["data"]}, scripts
        )
    path = tmp_path / "scripts.yaml"
    io.write_file(path, payload)
    config_manager.register_root(scripts, path)
    await scripts.activate()
    expected = "desktop" if legacy_children else "main"
    assert scripts[sid].Run.ExecutionSession == expected
    scripts[sid].Info.Name = "已保存托管"
    await scripts[sid].commit()
    await config_manager.flush()
    assert [str(uid) for uid in scripts[sid].users.keys()] == [first, second]
    assert scripts[sid].users[first].Task.Tasks == '{"daily":true}'
    assert scripts[sid].info.name == "已保存托管"
    config_manager.unregister_root(scripts)
    restored = ConfigCollection.build([WhimboxConfig])
    config_manager.register_root(restored, path)
    await restored.activate()
    assert [str(uid) for uid in restored[sid].users.keys()] == [first, second]
    assert restored[sid].users[second].info.name == second
    assert restored[sid].Run.ExecutionSession == (
        "desktop" if legacy_children else "main"
    )


@pytest.mark.asyncio
async def test_public_crud_restore_and_plugin_capabilities(
    monkeypatch, tmp_path_factory
):
    scripts = ConfigCollection.build([WhimboxConfig])
    await scripts.activate()
    monkeypatch.setattr(Config, "ScriptConfig", scripts)
    monkeypatch.setitem(config_manager._collections, "ScriptConfig", scripts)
    record = PluginRecord.build(
        info={"plugin_name": "whimbox_adapter", "state": PluginState.ENABLED}
    )
    record._plugin_cls = Adapter
    record._plugin = Adapter()
    monkeypatch.setattr(Plugin, "script_types", {record.uid: Adapter.decl})
    monkeypatch.setattr(Plugin, "list_plugins", lambda: [record])
    monkeypatch.setitem(Plugin._calls, record.uid, CallGate())
    await Adapter().pre_enable()
    from app.api import scripts_router

    app = FastAPI()
    app.include_router(scripts_router)
    app.include_router(Adapter.api_router)
    bind_openapi(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        types = (await client.get("/api/scripts/types")).json()["types"]
        assert "Whimbox" in types
        record.info.state = PluginState.DISABLING
        assert "Whimbox" not in (await client.get("/api/scripts/types")).json()["types"]
        record.info.state = PluginState.ENABLED
        created = (
            await client.post("/api/scripts/add", json={"type": "Whimbox"})
        ).json()
        assert created["code"] == 200, created
        sid = created["scriptId"]
        invalid_root = tmp_path_factory.mktemp("missing-executable")
        refused = (
            await client.post(
                "/api/scripts/update",
                json={
                    "scriptId": sid,
                    "data": {"Info": {"RootPath": str(invalid_root)}},
                },
            )
        ).json()
        assert refused["code"] != 200 and "whimbox_app.exe" in refused["message"]
        assert scripts[sid].Info.RootPath != invalid_root
        for elevated, expected in [(False, "admin_required"), (True, None)]:
            monkeypatch.setattr("app.utils.IS_ELEVATED", elevated)
            details = (
                await client.post("/api/scripts/get", json={"scriptId": sid})
            ).json()
            assert details["index"][0]["desktopSessionReason"] == expected
            assert details["index"][0]["desktopSessionSupported"] is elevated
        updated = (
            await client.post(
                "/api/scripts/update",
                json={
                    "scriptId": sid,
                    "data": {"Run": {"ExecutionSession": "desktop"}},
                },
            )
        ).json()
        assert updated["code"] == 200, updated
        assert scripts[sid].Run.ExecutionSession == "desktop"
        added = (
            await client.post("/api/scripts/user/add", json={"scriptId": sid})
        ).json()
        assert added["code"] == 200, added
        uid = added["userId"]
        detail = (
            await client.post(
                "/api/scripts/user/get", json={"scriptId": sid, "userId": uid}
            )
        ).json()
        assert detail["code"] == 200, detail
        assert detail["ui"]["backupTargets"] == [
            {"key": "mas", "kind": "user"},
            {"key": "native", "kind": "script"},
        ]
        assert [
            block["pageId"]
            for block in detail["ui"]["blocks"]
            if block["kind"] == "plugin"
        ] == ["tasks"]
        user = scripts[sid].users[uid]
        user.Task.Tasks = '{"daily":true}'
        await user.commit()
        prefix = "/api/plugin/whimbox_adapter/backup"
        context = {"scriptId": sid, "userId": uid, "target": "mas"}
        saved = (await client.post(prefix + "/ensure", json=context)).json()
        assert saved["code"] == 200 and saved["created"], saved
        user.Task.Tasks = "{}"
        await user.commit()
        selected = {**context, "time": saved["time"]}
        ticket = await scripts[sid].lock_s()
        try:
            assert (await client.post(prefix + "/restore", json=selected)).json()[
                "code"
            ] != 200
            assert user.Task.Tasks == "{}"
        finally:
            await scripts[sid].unlock(ticket)
        result = (await client.post(prefix + "/restore", json=selected)).json()
        assert result["code"] == 200, result
        assert json.loads(user.Task.Tasks) == {"daily": True}
        assert (await client.get(prefix + "/list", params=context)).json()["data"]
        assert (
            await client.get(
                prefix + "/file", params={**selected, "path": "../outside"}
            )
        ).json()["code"] == 400
        missing = (
            await client.get(
                prefix + "/list", params={**context, "userId": str(uuid4())}
            )
        ).json()
        assert missing["code"] == 400
        # A different managed script can share the same native config.
        from whimbox_adapter.tools import upstream
        from whimbox_adapter.tools.update import installation_access

        root = tmp_path_factory.mktemp("whimbox-install")
        other_id = scripts.add(WhimboxConfig, payload={"Info": {"RootPath": str(root)}})
        (root / "whimbox_app.exe").touch()
        scripts[sid].Info.RootPath = root
        await scripts.commit()
        await scripts[sid].commit()
        other_user = (
            await client.post("/api/scripts/user/add", json={"scriptId": str(other_id)})
        ).json()
        assert other_user["code"] == 200, other_user
        surface = upstream.WheelAssetsConfigSurface(root)
        surface.config_path.parent.mkdir()
        surface.config_path.write_bytes(b'{"before": true}')
        native = {**context, "target": "native"}
        snapshot = (await client.post(prefix + "/ensure", json=native)).json()
        assert snapshot["code"] == 200 and snapshot["created"], snapshot
        surface.config_path.write_bytes(b'{"running": true}')
        restore = {
            **native,
            "scriptId": str(other_id),
            "userId": other_user["userId"],
            "time": snapshot["time"],
        }
        with installation_access(scripts[sid].Info.RootPath):
            blocked = (await client.post(prefix + "/restore", json=restore)).json()
            assert blocked["code"] != 200, blocked
            assert surface.config_path.read_bytes() == b'{"running": true}'
        native_result = (await client.post(prefix + "/restore", json=restore)).json()
        assert native_result["code"] == 200, native_result
        assert surface.config_path.read_bytes() == b'{"before": true}'
        # An explicit restore can clear a pending overlay without discarding new edits.
        monkeypatch.setattr(surface, "_read_template_only", lambda: {})
        with installation_access(root):
            surface.materialize_overrides({}, {}, run_all_accounts=False)
        manual_edit = b'{"edited": true}'
        surface.config_path.write_bytes(manual_edit)
        stopped = AsyncMock(return_value=False)
        monkeypatch.setattr(upstream.process_tools, "is_process_stopped", stopped)
        blocked = (await client.post(prefix + "/restore", json=restore)).json()
        assert blocked["code"] != 200, blocked
        assert surface.config_path.read_bytes() == manual_edit
        assert surface.has_pending_overlay()
        stopped.return_value = True
        native_result = (await client.post(prefix + "/restore", json=restore)).json()
        assert native_result["code"] == 200, native_result
        assert surface.config_path.read_bytes() == b'{"before": true}'
        assert manual_edit in [
            entry.read_bytes() for entry in surface.backup_root().glob("*/config.json")
        ]
        assert not surface.has_pending_overlay()
        # Both the legacy editor and the shared editor read the original snapshots.
        assert (await client.get("/api/scripts/backup/list", params=context)).json()[
            "code"
        ] == 200
        schema = app.openapi()["components"]["schemas"]
        assert "WhimboxConfig" in schema and "WhimboxUserConfig" in schema
        assert Adapter.decl.activity_game == "infinitynikki"
        from app.core.mcp import included_operations

        assert (
            "get_whimbox_task_catalog_api_api_scripts_whimbox_task_catalog_get"
            in included_operations(app)
        )
