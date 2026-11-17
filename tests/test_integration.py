"""Guard saved-account identity and the public plugin editing contract."""

import json
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from whimbox_adapter.config import WhimboxConfig
from whimbox_adapter.plugin import Plugin as Adapter

from app.config import ConfigCollection, config_manager
from app.core import Config, Plugin
from app.core.config.upgrade import convert_legacy
from app.models.config import PluginRecord
from app.plugin.openapi import bind_openapi
from app.plugin.types import PluginState
from app.utils import io


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
    await config_manager.flush()
    assert [str(uid) for uid in scripts[sid].users.keys()] == [first, second]
    assert scripts[sid].users[first].Task.Tasks == '{"daily":true}'
    assert scripts[sid].info.name == "原托管"
    config_manager.unregister_root(scripts)
    restored = ConfigCollection.build([WhimboxConfig])
    config_manager.register_root(restored, path)
    await restored.activate()
    assert [str(uid) for uid in restored[sid].users.keys()] == [first, second]
    assert restored[sid].users[second].info.name == second


@pytest.mark.asyncio
async def test_public_crud_restore_and_plugin_capabilities(monkeypatch):
    scripts = ConfigCollection.build([WhimboxConfig])
    await scripts.activate()
    monkeypatch.setattr(Config, "ScriptConfig", scripts)
    monkeypatch.setitem(config_manager._collections, "ScriptConfig", scripts)
    record = PluginRecord.build(
        info={"plugin_name": "whimbox_adapter", "state": PluginState.ENABLED}
    )
    record._plugin_cls = Adapter
    monkeypatch.setattr(Plugin, "script_types", {record.uid: Adapter.decl})
    monkeypatch.setattr(Plugin, "list_plugins", lambda: [record])
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
        added = (
            await client.post("/api/scripts/user/add", json={"scriptId": sid})
        ).json()
        assert added["code"] == 200, added
        uid = added["userId"]
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
        # No built-in restore pool remains after migration.
        assert (await client.get("/api/scripts/backup/list", params=context)).json()[
            "code"
        ] != 200
        schema = app.openapi()["components"]["schemas"]
        assert "WhimboxConfig" in schema and "WhimboxUserConfig" in schema
        assert Adapter.decl.activity_game == "infinitynikki"
        from app.core.mcp import included_operations

        assert (
            "get_whimbox_task_catalog_api_api_scripts_whimbox_task_catalog_get"
            in included_operations(app)
        )
