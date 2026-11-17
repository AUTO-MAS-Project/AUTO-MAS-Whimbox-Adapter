"""Run real L3/L4 lifecycles with isolated process and upstream-file boundaries."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from app.config import ConfigCollection
from app.models.task import TaskItem, TaskScriptItem, TaskUserItem
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

        def snapshot_pre_run(self):
            return "before"

        def materialize_overrides(self, *args, **kwargs):
            path.write_text('{"overlay":true}')

        def restore_pre_run(self, anchor):
            assert anchor == "before"
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
