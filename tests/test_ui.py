"""验证宿主的编辑块契约和奇想盒面板产物。"""

import json
import subprocess
from pathlib import Path

from app.api import plugin_router
from app.api._config_editors import config_detail_ui
from app.config import ConfigCollection, config_manager
from app.core import Config, Plugin
from app.core.i18n import I18nManager
from app.models.config import PluginRecord, PluginRegistryCollection
from app.models.schema import DeclarativeBlockOut, HostBlockOut, PluginBlockOut
from app.plugin.calls import CallGate
from app.plugin.frontend import MountedFrontend
from app.plugin.http import HttpRegistry
from app.plugin.types import PluginState
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import whimbox_adapter
from whimbox_adapter.config import WhimboxConfig
from whimbox_adapter.plugin import Plugin as Adapter
from whimbox_adapter.tools.upstream import (
    APP_EXE_NAME,
    MATERIAL_FILE,
    OPTIONS_SECTION,
    REL_ASSETS_DIR,
    REL_PYTHON_EXE,
    REL_SITE_PACKAGES,
    SETTING_OPTIONS_FILE,
    STEPS_SECTION,
    TEMPLATE_FILE,
)

PACKAGE = Path(whimbox_adapter.__file__).resolve().parent
UI = PACKAGE / "ui"


async def test_blocks_catalog_and_mounted_assets(monkeypatch, tmp_path_factory):
    scripts = ConfigCollection.build([WhimboxConfig])
    await scripts.activate()
    monkeypatch.setattr(Config, "ScriptConfig", scripts)
    monkeypatch.setitem(config_manager._collections, "ScriptConfig", scripts)
    registry = PluginRegistryCollection.build()
    await registry.activate()
    record_uid = registry.add(
        PluginRecord,
        payload={
            "info": {"plugin_name": "whimbox_adapter", "state": PluginState.ENABLED}
        },
    )
    await registry.commit()
    monkeypatch.setattr(Config, "plugin_registry", registry)
    record = registry[record_uid]
    record._plugin_cls = Adapter
    monkeypatch.setattr(Plugin, "script_types", {record.uid: Adapter.decl})
    gate = CallGate()
    monkeypatch.setitem(Plugin._calls, record.uid, gate)
    frontend = MountedFrontend.load(
        PACKAGE, "whimbox_adapter", "Whimbox", None, gate, block_pages={"tasks"}
    )
    assert frontend is not None
    assert not frontend.diagnostics and not frontend.bindings
    monkeypatch.setattr(Plugin, "http", HttpRegistry())
    mount = Plugin.http.mount("whimbox_adapter", [], frontend=frontend)

    app = FastAPI()
    app.include_router(plugin_router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assets = f"{mount.prefix}/ui/{frontend.revision}"
        for name in (
            "tasks.html",
            "tasks.js",
            "tasks.css",
            "_host/panel-sdk.js",
            "_host/panel-base.css",
        ):
            response = await client.get(f"{assets}/{name}")
            assert response.status_code == 200, name
        html = (await client.get(f"{assets}/tasks.html")).text
        assert 'src="./_host/panel-sdk.js"' in html
        assert 'href="./_host/panel-base.css"' in html

    await Adapter().pre_enable()
    script_uid, script = await Config.add_script("Whimbox")
    script_id = str(script_uid)
    _, documents = await Config.get_script(script_id)
    ui = config_detail_ui(script, documents)
    assert ui.blocks == [
        DeclarativeBlockOut(groups=["Info"]),
        HostBlockOut(name="game_binding"),
        DeclarativeBlockOut(groups=["Run", "Update"]),
    ]
    assert not ui.backupTargets and ui.nativeSession is None

    user_uid, user = await Config.add_user(script_id)
    user_id = str(user_uid)
    _, documents = await Config.get_user(script_id, user_id)
    ui = config_detail_ui(user, documents)
    assert ui.blocks == [
        HostBlockOut(name="config_restore"),
        HostBlockOut(name="native_session"),
        DeclarativeBlockOut(groups=["Info", "OneDragon"]),
        PluginBlockOut(
            pageId="tasks",
            fallbackGroups=["Task"],
            mount=frontend.surface_mount(name="whimbox_adapter", page="tasks"),
        ),
        DeclarativeBlockOut(groups=["Notify"]),
        HostBlockOut(name="webhooks"),
    ]
    assert [(pool.key, pool.kind) for pool in ui.backupTargets] == [
        ("mas", "user"),
        ("native", "script"),
    ]
    assert ui.nativeSession == "config"
    assert {hint["field"]: hint["editable"] for hint in ui.hints["Task"]} == {
        "Catalog": False,
        "Tasks": True,
        "Options": True,
    }
    assert json.loads(documents[user_id]["Task"]["Catalog"])["error"]

    root = tmp_path_factory.mktemp("whimbox-install")
    upstream = root / REL_SITE_PACKAGES / REL_ASSETS_DIR
    upstream.mkdir(parents=True)
    (root / APP_EXE_NAME).touch()
    (root / REL_PYTHON_EXE).touch()
    for filename, payload in {
        TEMPLATE_FILE: {
            STEPS_SECTION: {"daily": {"description": "Daily"}},
            OPTIONS_SECTION: {"target": {"description": "Target", "value": "a"}},
        },
        SETTING_OPTIONS_FILE: {"target": ["a", "b"]},
        MATERIAL_FILE: {},
    }.items():
        (upstream / filename).write_text(json.dumps(payload), encoding="utf-8")
    await Config.update_script(script_id, {"Info": {"RootPath": str(root)}})
    await Config.update_user(script_id, user_id, {"Task": {"Tasks": '{"daily":false}'}})
    _, documents = await Config.get_user(script_id, user_id)
    task = documents[user_id]["Task"]
    assert json.loads(task["Tasks"]) == {"daily": False}
    catalog = json.loads(task["Catalog"])
    assert "error" not in catalog, catalog
    assert catalog["steps"][0]["key"] == "daily"
    assert catalog["options"][0]["options"] == ["a", "b"]
    assert "Catalog" not in user.model_dump(context={"audience": "persist"})["Task"]


async def test_panel_javascript_and_catalog_alignment():
    subprocess.run(["node", "--check", str(UI / "tasks.js")], check=True)
    manager = I18nManager(Config.resource_path / "i18n")
    manager.set_package_roots({"whimbox_adapter": PACKAGE})
    catalogs = []
    for locale in ("zh-CN", "en-US", "ja-JP"):
        catalog = json.loads(
            (PACKAGE / "i18n" / f"{locale}.json").read_text(encoding="utf-8")
        )
        assert all(
            key.startswith("plugins.whimbox_adapter.") and value
            for key, value in catalog.items()
        )
        catalogs.append(catalog.keys())
        messages, _ = await manager.get_messages(locale)
        plugin = messages["plugins"]["whimbox_adapter"]
        assert (
            plugin["panel"]["tasks"]["title"]
            == catalog["plugins.whimbox_adapter.panel.tasks.title"]
        )
        assert (
            plugin["config"]["Info"]["Base"]["options"]["native"]
            == catalog["plugins.whimbox_adapter.config.Info.Base.options.native"]
        )
    assert catalogs[0] == catalogs[1] == catalogs[2]
