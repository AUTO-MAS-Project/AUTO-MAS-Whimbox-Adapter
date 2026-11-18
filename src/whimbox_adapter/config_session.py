#   AUTO-MAS: A Multi-Script, Multi-Config Management and Automation Software
#   Copyright © 2025-2026 AUTO-MAS Team
#
#   This file is part of AUTO-MAS.
#
#   AUTO-MAS is free software: you can redistribute it and/or modify
#   it under the terms of the GNU Affero General Public License as
#   published by the Free Software Foundation, either version 3 of
#   the License, or (at your option) any later version.
#
#   AUTO-MAS is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#   GNU Affero General Public License for more details.
#
#   You should have received a copy of the GNU Affero General Public License
#   along with AUTO-MAS. If not, see <https://www.gnu.org/licenses/>.

"""奇想盒原生来源配置：无参数拉起 whimbox_app.exe，用户在原生 GUI 修改设置。

MAS 不预置不回读（BetterGI「GUI 原生来源只打开本体」同款先例）；会话进入/退出
各归档一次 config.json（通用恢复池 snapshot，指纹去重），捕捉用户经 app 的
原生修改前后态，作为跨会话恢复锚点。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

from auto_mas_core import ScriptConfigWorker
from auto_mas_core.host.core import Publisher
from auto_mas_core.host.core_ws import protocol
from auto_mas_core.host.models_schema import WSTaskNoticeData
from auto_mas_core.host.task_proxy_helpers import push_dispatch_log
from auto_mas_core.host.utils import (
    IS_ELEVATED,
    ProcessInfo,
    ProcessManager,
    get_logger,
)

from .tools.launcher import launch_installation_process, stop_installation_processes
from .tools.upstream import APP_EXE_NAME, REL_PYTHON_EXE, WheelAssetsConfigSurface

logger = get_logger("奇想盒 脚本设置")


class ScriptConfigTask(ScriptConfigWorker):
    """无参数启动奇想盒 app，供用户修改程序设置（原生 GUI 原生来源）。"""

    def __init__(self, expander, user, config):
        """绑定配置会话的运行节点，原生 GUI 使用托管配置目录。"""
        super().__init__(expander, user, config)
        self.script_config = expander.script_config
        self.cur_user_item = user
        self.process_manager = ProcessManager()
        self.wait_event = asyncio.Event()
        self.root_path = Path(str(self.script_config.Info.RootPath or ""))
        self.exe_path = self.root_path / APP_EXE_NAME
        self.target_process = ProcessInfo(
            name=APP_EXE_NAME, exe=str(self.exe_path), cmdline=None
        )
        self.config_surface = WheelAssetsConfigSurface(self.root_path)
        self._launch_attempted = False

    async def main_logic(self) -> None:
        await self.cur_user_item.set_state(status="运行")
        # 会话进入时归档（捕捉「用户在 app 内修改前」的原生配置态）
        with suppress(Exception):
            self.config_surface.snapshot_entry()
        await push_dispatch_log(self.script_info, "启动奇想盒 app（原生来源配置）")
        # 仅当 MAS 自身未提权时才走 runas 触发 UAC；已提权时子进程自动继承
        self._launch_attempted = True
        await launch_installation_process(
            self.process_manager,
            self.root_path,
            self.exe_path,
            elevated=bool(self.script_config.Run.UseAdmin) and (not IS_ELEVATED),
            target_process=self.target_process,
        )
        await self.wait_event.wait()

    async def finish(self) -> None:
        self.wait_event.set()
        await self._kill_processes()
        # 会话退出时归档（固化用户在 app 中已保存的编辑，作为恢复锚点）
        with suppress(Exception):
            self.config_surface.snapshot_entry()
        if self.error is None:
            logger.success("奇想盒原生来源配置会话已结束")
            await self.cur_user_item.set_state(status="完成")

    async def on_crash(self, e: Exception) -> None:
        await super().on_crash(e)
        logger.opt(exception=True).warning(f"奇想盒设置任务出现异常: {e}")
        await Publisher.send(
            id=str(self.task_info.uid),
            type=protocol.TASK_NOTICE,
            data=WSTaskNoticeData(
                level="error", message=f"奇想盒设置任务出现异常: {e}"
            ),
        )

    async def _kill_processes(self) -> None:
        if self._launch_attempted:
            await stop_installation_processes(
                self.process_manager,
                self.root_path,
                self.exe_path,
                self.root_path / REL_PYTHON_EXE,
            )
