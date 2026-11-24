"""Run real L3/L4 lifecycles with isolated process and upstream-file boundaries."""

import asyncio
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from app.config import ConfigCollection
from app.models.task import TaskItem, TaskScriptItem, TaskUserItem
from app.services import script_install
from app.utils import UTC4

from whimbox_adapter.auto_proxy import AutoProxyTask
from whimbox_adapter.config import WhimboxConfig, WhimboxUserConfig
from whimbox_adapter.contracts import WhimboxRunEvent
from whimbox_adapter.manager import WhimboxManager


async def runtime(tmp_path, *, mode="AutoProxy"):
    scripts = ConfigCollection.build([WhimboxConfig])
    await scripts.activate()
    sid = scripts.add(
        WhimboxConfig,
        payload={
            "Info": {"RootPath": str(tmp_path)},
            "Run": {"UseAdmin": False, "RunTimesLimit": 1},
        },
    )
    await scripts.commit()
    config = scripts[sid]
    uid = config.users.add(
        WhimboxUserConfig, payload={"Info": {"Status": True, "RemainedDay": 5}}
    )
    await config.users.commit()
    root = TaskItem(info={"mode": mode})
    await root.activate()
    rid = root.scripts.add(
        TaskScriptItem,
        payload={
            "info": {"script_id": str(sid), "name": "Whimbox", "script_type": "Whimbox"}
        },
    )
    await root.scripts.commit()
    script = root.scripts[rid]
    user_id = script.users.add(
        TaskUserItem, payload={"info": {"user_id": str(uid), "name": "User"}}
    )
    await script.users.commit()
    await root.set_current_index(0)
    expander = WhimboxManager(script, config)
    expander.history = SimpleNamespace(
        begin_user_history_capture=lambda *a, **k: None, save_user_history=AsyncMock()
    )
    return expander, script.users[user_id], config.users[uid]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["success", "cancel", "full_counter", "after_script_error"]
)
async def test_proxy_result_or_cancel_restores_before_releasing_user_lock(
    tmp_path, tmp_path_factory, monkeypatch, case
):
    cancel = case == "cancel"
    expander, user, config = await runtime(tmp_path_factory.mktemp("whimbox-install"))
    from whimbox_adapter import auto_proxy, manager

    monkeypatch.setattr(
        manager, "Notify", SimpleNamespace(push_proxy_result=AsyncMock())
    )
    monkeypatch.setattr(
        auto_proxy,
        "Notify",
        SimpleNamespace(push_statistics_report=AsyncMock(), send_notice=AsyncMock()),
    )
    monkeypatch.setattr(expander, "_prepare_update", AsyncMock())
    if case == "full_counter":
        config.Data.ProxyTimes = 9999
        config.Data.LastProxyDate = datetime.now(tz=UTC4)
        await config.commit()
    if case == "after_script_error":
        from app.services.common.custom_script import execute_script_task

        script_file = tmp_path_factory.mktemp("custom-script") / "after.cmd"
        script_file.write_text("exit 0")
        config.Info.IfScriptAfterTask = True
        config.Info.ScriptAfterTask = script_file
        await config.commit()
        assert config.Info.ScriptAfterTask == script_file

        async def inaccessible_script(path, task_name):
            # Network/permission failure in the actual executor's preflight.
            with patch.object(
                type(path), "is_file", side_effect=OSError("unavailable")
            ):
                return await execute_script_task(path, task_name)

        monkeypatch.setattr(auto_proxy, "execute_script_task", inaccessible_script)
    launcher = SimpleNamespace(
        spawn=AsyncMock(),
        terminate=AsyncMock(),
        is_running=AsyncMock(return_value=True),
    )
    original = '{"original":true}'
    path = tmp_path / "upstream.json"
    path.write_text(original)
    entered = asyncio.Event()
    restored = []

    class Surface:
        def check_install(self):
            return None

        def snapshot_entry(self):
            pass

        def materialize_overrides(self, *args, **kwargs):
            path.write_text('{"overlay":true}')

        async def restore_pending_overlay(self, *, finishing_run=False):
            restored.append(config.is_locked)
            path.write_text(original)

    class Feed:
        latest_time = datetime.now()
        log_text = "一条龙任务完成: 任务结果如下：\n✅ daily\n任务结束，程序退出\n"

        def __init__(self, callback):
            self.callback = callback

        async def start(self, started):
            entered.set()
            if not cancel:
                # Also guards the terminal event arriving before start() returns.
                await self.callback(WhimboxRunEvent(text=self.log_text))

        async def stop(self):
            pass

    worker = AutoProxyTask(
        expander,
        user,
        config,
        config_surface=Surface(),
        launcher=launcher,
        feed_factory=Feed,
    )
    monkeypatch.setattr(expander, "build_worker", lambda *args: worker)
    expander.start()
    await asyncio.wait_for(entered.wait(), 5)
    if cancel:
        await asyncio.wait_for(expander.stop(), 5)
    else:
        await asyncio.wait_for(expander.accomplish.wait(), 8)
    assert path.read_text() == original
    assert restored == [True]
    assert not config.is_locked
    launcher.terminate.assert_awaited_once()
    assert expander.error is None
    if case == "full_counter":
        assert worker.error is not None
        assert config.Data.ProxyTimes == 9999
        assert user.info.status == "异常"
        return
    assert worker.error is None
    expander.history.save_user_history.assert_awaited_once()
    assert user.info.status == ("已取消" if cancel else "完成")
    assert config.Data.ProxyTimes == (0 if cancel else 1)
    assert config.Info.RemainedDay == (5 if cancel else 4)
    if not cancel:
        assert config.Data.LastProxyDate.date() == datetime.now(tz=UTC4).date()


@pytest.mark.asyncio
async def test_incremental_feed_commits_log_content_and_retains_partial_lines(
    tmp_path, tmp_path_factory
):
    from whimbox_adapter.tools.feed import LoguruFileFeed

    expander, user, config = await runtime(tmp_path_factory.mktemp("whimbox-install"))
    worker = AutoProxyTask(expander, user, config)
    await worker.prepare()
    await expander.script_info.set_current_index(0)
    worker.cur_user_log = await worker.add_log(datetime.now().astimezone())
    worker.feed = LoguruFileFeed(worker._on_run_event, lambda: tmp_path / "log.txt")
    changes = []
    committing = asyncio.Event()
    release = asyncio.Event()

    async def changed(_sender, event):
        changes.append((list(event.old_value), list(event.value)))
        if len(changes) == 1:
            committing.set()
            await release.wait()

    worker.cur_user_log.connect(changed, kind="set", group="info", field="content")
    now = datetime.now().astimezone()
    incoming = asyncio.create_task(worker.feed._on_log(["partial"], now))
    await asyncio.wait_for(committing.wait(), 2)
    # The real exit watcher can replay the same collected batch during commit.
    replay = asyncio.create_task(worker._evaluate_and_release(refresh_pending=True))
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(incoming, replay), 2)
    assert worker._log_consumed_lines == worker.feed.revision
    await worker.feed._on_log(["partial", " tail\n"], now)
    await worker.feed._on_log(["partial", " tail\n"], now)
    assert changes == [([], ["partial"]), (["partial"], ["partial tail\n"])]
    assert worker.cur_user_log.info.content == ["partial tail\n"]
    assert worker._log_consumed_lines == worker.feed.revision


@pytest.mark.parametrize("exit_watcher", [False, True])
async def test_terminal_log_batch_collects_failure_reasons(tmp_path, exit_watcher):
    from whimbox_adapter.tools.feed import LoguruFileFeed

    expander, user, config = await runtime(tmp_path)
    worker = AutoProxyTask(expander, user, config)
    await worker.prepare()
    await expander.script_info.set_current_index(0)
    now = datetime.now().astimezone()
    worker.cur_user_log = await worker.add_log(now)
    callback = (
        AsyncMock(side_effect=OSError()) if exit_watcher else worker._on_run_event
    )
    worker.feed = LoguruFileFeed(callback, lambda: tmp_path / "log.txt")

    def line(message):
        return f"{now:%Y-%m-%d %H:%M:%S}.000 | INFO | main - {message}\n"

    batch = [
        line("开始任务"),
        line("❌ 登录失败"),
        line("🛑 网络超时"),
        line("❌ 登录失败"),
        line("❌ 正在挖掘，无法收获"),
        line("一条龙任务完成: 任务结果如下："),
        "❌ 某个任务未完成\n",
        line("任务结束，程序退出"),
    ]
    if exit_watcher:
        with pytest.raises(OSError):
            await worker.feed._on_log(batch, now)
        await worker._evaluate_and_release(refresh_pending=True)
    else:
        first_part, rest = batch[1].split("录", 1)
        await worker.feed._on_log([batch[0], first_part], now)
        batch = [batch[0], first_part, "录" + rest, *batch[2:]]
        await worker.feed._on_log(batch, now)
    assert worker.wait_event.is_set()
    assert worker.cur_user_log.info.status == "Success!"
    assert worker.failure_notes == ["❌ 登录失败", "🛑 网络超时"]


async def test_unconfirmed_stop_preserves_overlay_and_reserves_install(
    tmp_path_factory, monkeypatch
):
    from whimbox_adapter import manager
    from whimbox_adapter.tools import launcher
    from whimbox_adapter.tools.upstream import WheelAssetsConfigSurface

    root = tmp_path_factory.mktemp("whimbox-install")
    expander, user, config = await runtime(root)
    expander.script_config.Run.RunTimesLimit = 2
    await expander.script_config.commit()
    monkeypatch.setattr(expander, "_prepare_update", AsyncMock())
    monkeypatch.setattr(
        manager, "Notify", SimpleNamespace(push_proxy_result=AsyncMock())
    )
    # The production reservation intentionally survives until the host exits.
    retained = {}
    monkeypatch.setattr(script_install, "_deferred_locks", retained)
    monkeypatch.setattr(script_install, "_pending_processes", {})

    surface = WheelAssetsConfigSurface(root)
    original = {"OneDragon": {"auto_close_game": {"value": "false"}}}
    surface.config_path.parent.mkdir()
    surface.config_path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(surface, "check_install", lambda: None)
    monkeypatch.setattr(surface, "_read_template_only", lambda: original)

    class Feed:
        latest_time = datetime.now()
        log_text = "一条龙任务完成: 网络超时\n任务结束，程序退出\n"

        def __init__(self, callback):
            self.callback = callback

        async def start(self, started):
            await self.callback(WhimboxRunEvent(text=self.log_text))

        async def stop(self):
            pass

    process = launcher.EmbeddedPythonLauncher(root, use_admin=False)
    monkeypatch.setattr(process.process_manager, "open_process", AsyncMock())
    monkeypatch.setattr(
        process.process_manager, "kill", AsyncMock(side_effect=PermissionError())
    )
    monkeypatch.setattr(
        launcher.process_tools, "kill_process", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        launcher.process_tools, "is_process_stopped", AsyncMock(return_value=False)
    )
    worker = AutoProxyTask(
        expander,
        user,
        config,
        config_surface=surface,
        launcher=process,
        feed_factory=Feed,
    )
    monkeypatch.setattr(worker, "run_user_scripts_after", AsyncMock())
    monkeypatch.setattr(manager, "AutoProxyTask", lambda *_: worker)

    try:
        await asyncio.wait_for(expander.run(), 5)
        assert user.info.status == "异常"
        assert not config.is_locked
        process.process_manager.open_process.assert_awaited_once()
        worker.run_user_scripts_after.assert_not_awaited()
        assert (
            json.loads(surface.config_path.read_text(encoding="utf-8"))["OneDragon"][
                "auto_close_game"
            ]["value"]
            == "true"
        )
        assert [
            json.loads(path.read_bytes())
            for path in surface.backup_root().glob("*/config.json")
        ] == [original]
        with pytest.raises(script_install.InstallUnsafeError):
            expander.build_worker(user, config)
        with pytest.raises(script_install.InstallUnsafeError):
            with manager.installation_access(root):
                pytest.fail(
                    "The shared installation became available while its writer is unconfirmed"
                )
        assert retained
    finally:
        for handles in retained.values():
            for handle in handles:
                script_install._release_access(handle)


@pytest.mark.asyncio
async def test_native_gui_start_failure_remains_failure(tmp_path_factory, monkeypatch):
    from whimbox_adapter import config_session

    expander, user, config = await runtime(
        tmp_path_factory.mktemp("whimbox-install"), mode="ScriptConfig"
    )
    worker = config_session.ScriptConfigTask(expander, user, config)
    monkeypatch.setattr(
        worker.process_manager,
        "open_process",
        AsyncMock(side_effect=OSError("launch failed")),
    )
    monkeypatch.setattr(worker, "_kill_processes", AsyncMock())
    monkeypatch.setattr(config_session.Publisher, "send", AsyncMock())
    await worker.run()
    assert isinstance(worker.error, OSError)
    assert user.info.status == "异常"
    worker._kill_processes.assert_awaited_once()


@pytest.mark.parametrize("had_original", [True, False])
async def test_failed_restore_blocks_reuse_until_recovered(
    tmp_path_factory, monkeypatch, had_original
):
    from whimbox_adapter import manager
    from whimbox_adapter.tools import upstream

    root = tmp_path_factory.mktemp("whimbox-install")
    expander, user, config = await runtime(root)
    surface = upstream.WheelAssetsConfigSurface(root)
    path = surface.config_path
    baseline = b'{ "OneDragon": {"auto_close_game": {"value": "false"}} }\r\n'
    path.parent.mkdir()
    if had_original:
        path.write_bytes(baseline)
    monkeypatch.setattr(surface, "check_install", lambda: None)
    monkeypatch.setattr(surface, "_read_template_only", lambda: json.loads(baseline))
    monkeypatch.setattr(
        upstream.process_tools, "is_process_stopped", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        manager, "Notify", SimpleNamespace(push_proxy_result=AsyncMock())
    )
    monkeypatch.setattr(expander, "_prepare_update", AsyncMock())

    log_text = "一条龙任务完成: 任务结果如下：\n✅ daily\n任务结束，程序退出\n"

    def feed_factory(callback):
        async def start(_started):
            await callback(WhimboxRunEvent(text=log_text))

        return SimpleNamespace(
            start=start, stop=AsyncMock(), latest_time=datetime.now(), log_text=log_text
        )

    # Simulate a config file that cannot be replaced or deleted after the process exits.
    locked = False

    async def terminate():
        nonlocal locked
        # The upstream process can change the overlay during a normal run.
        current = json.loads(path.read_bytes())
        current["RunState"] = {"written_by_whimbox": True}
        path.write_text(json.dumps(current), encoding="utf-8")
        locked = True

    original_write = upstream.atomic_write
    original_unlink = type(path).unlink

    def write(target, content):
        if target == path and locked:
            raise PermissionError("config file is locked")
        original_write(target, content)

    def unlink(target, *args, **kwargs):
        if target == path and locked:
            raise PermissionError("config file is locked")
        original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(upstream, "atomic_write", write)
    monkeypatch.setattr(type(path), "unlink", unlink)
    worker = AutoProxyTask(
        expander,
        user,
        config,
        config_surface=surface,
        launcher=SimpleNamespace(
            spawn=AsyncMock(),
            terminate=terminate,
            is_running=AsyncMock(return_value=True),
        ),
        feed_factory=feed_factory,
    )
    monkeypatch.setattr(worker, "run_user_scripts_after", AsyncMock())
    monkeypatch.setattr(manager, "AutoProxyTask", lambda *_: worker)

    await asyncio.wait_for(expander.run(), 8)
    assert user.info.status == "异常"
    assert config.Data.ProxyTimes == 0
    worker.run_user_scripts_after.assert_not_awaited()
    assert (
        json.loads(path.read_bytes())["OneDragon"]["auto_close_game"]["value"] == "true"
    )
    with pytest.raises(script_install.InstallUnsafeError):
        expander.build_worker(user, config)

    # A newly created manager must use recovery data from disk.
    retry = WhimboxManager(expander.script_info, expander.script_config)
    monkeypatch.setattr(retry, "_prepare_update", AsyncMock())
    try:
        with pytest.raises(script_install.InstallUnsafeError):
            await retry.prepare()
        retry._prepare_update.assert_not_awaited()
    finally:
        await retry.finish()

    locked = False
    last_overlay = path.read_bytes()
    manual_edit = b'{"changed": true}'
    path.write_bytes(manual_edit)
    retry = WhimboxManager(expander.script_info, expander.script_config)
    try:
        with pytest.raises(script_install.InstallUnsafeError):
            await retry.prepare()
        assert path.read_bytes() == manual_edit
    finally:
        await retry.finish()

    path.write_bytes(last_overlay)
    next_run = WhimboxManager(expander.script_info, expander.script_config)
    monkeypatch.setattr(next_run, "_prepare_update", AsyncMock())
    try:
        await next_run.prepare()
        next_run._prepare_update.assert_awaited_once()
        if had_original:
            assert path.read_bytes() == baseline
        else:
            assert not path.exists()
        assert not surface.has_pending_overlay()
    finally:
        await next_run.finish()


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop process scope")
@pytest.mark.asyncio
async def test_desktop_cleanup_preserves_another_process_using_the_installation(
    tmp_path,
):
    from app.utils.common.process.session import (
        DesktopProcessTarget,
        desktop_process_target,
    )

    from whimbox_adapter.tools import launcher

    exe = tmp_path / launcher.REL_PYTHON_EXE
    exe.parent.mkdir(parents=True)
    shutil.copy2(Path(os.environ["SystemRoot"]) / "System32/ping.exe", exe)
    process = subprocess.Popen(
        [exe, "-t", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    token = desktop_process_target.set(DesktopProcessTarget(99, tmp_path, tmp_path))
    try:
        assert process.pid in launcher.find_upstream_pids(tmp_path)
        manager = AsyncMock()
        with pytest.raises(launcher.ProcessCleanupUnconfirmed):
            await launcher.stop_installation_processes(manager, tmp_path, exe)
        assert process.poll() is None
    finally:
        desktop_process_target.reset(token)
        process.kill()
        await asyncio.to_thread(process.wait, 5)
