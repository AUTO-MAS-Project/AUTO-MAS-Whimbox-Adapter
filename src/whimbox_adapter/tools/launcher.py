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

"""IProcessLauncher 默认实现 + 上游进程探测。

上游拉起事实：``spawn(<root>/python-embedded/python.exe, ['-s', '-m',
'whimbox.main'], {cwd: <root>})``，环境 PATH 前插 python-embedded 且
``PYTHONHOME=<root>/python-embedded``。**事实来源分级**：CLI 入口（含
``startOneDragon`` 分支）可在上游 Python 仓内核对（v3.0.5→v3.0.7 未改）；
env 与 spawn 布局来自发行版安装目录，Electron 壳源码不在该仓内、无法从上游
git 复核，按安装布局实证。MAS 无头拉起复刻同一入口（CLI 入口属「允许依赖」
白名单）。

提权与 env 的边界：``ProcessManager.open_process`` 的 elevated 分支走
ShellExecute（触发 UAC），不支持自定义 env；MAS 自身已提权时子进程继承
管理员令牌，走普通 spawn 分支、可带 env。两条路径下嵌入式 Python 的
home 解析以 ``python312._pth`` 布局为主，env 仅防御性复刻 app 行为。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import psutil
from auto_mas_core.adapters.script import in_desktop_session
from auto_mas_core.host.services import script_install
from auto_mas_core.host.utils import (
    IS_ELEVATED,
    ProcessInfo,
    ProcessManager,
    get_logger,
)
from auto_mas_core.host.utils import process as process_tools
from auto_mas_core.host.utils_common_process_runner import (
    ProcessCleanupUnconfirmed,
    is_process_cleanup_unconfirmed,
)

from whimbox_adapter.tools.upstream import APP_EXE_NAME, REL_PYTHON_EXE

logger = get_logger("奇想盒 进程启动")

# 无头一条龙 CLI 入口（上游 main.py:83-90，自 tag 1.3.6 起存在）
WHIMBOX_CLI_ARGS: tuple[str, ...] = ("-s", "-m", "whimbox.main", "startOneDragon")


async def launch_installation_process(
    manager: ProcessManager,
    root: Path,
    program: Path,
    *args: str,
    target_process: ProcessInfo | None,
    elevated: bool,
    env: dict[str, str] | None = None,
) -> None:
    """启动未确认收回时，阻止任务收尾改写同一安装。"""
    try:
        await manager.open_process(
            program,
            *args,
            cwd=root,
            target_process=target_process,
            elevated=elevated,
            retain_desktop_descendants=in_desktop_session(),
            env=env,
        )
    except (Exception, asyncio.CancelledError) as error:
        # UAC 在宿主执行器线程中等待，同步调用可能在任务取消后才完成启动。
        if is_process_cleanup_unconfirmed(error) or (
            elevated and isinstance(error, asyncio.CancelledError)
        ):
            script_install.retain_installation_until_restart(root)
        raise


async def stop_installation_processes(
    manager: ProcessManager, root: Path, *executables: Path
) -> None:
    """同时确认进程树中止结果与镜像路径；异常时保留安装占用。"""
    script_install.assert_no_pending_process(root)
    stopped = True
    try:
        if in_desktop_session():
            # 分身启动句柄持有本次任务进程树；同路径的外部进程仅参与占用核验。
            await manager.kill()
            for executable in executables:
                if not await process_tools.is_process_stopped(
                    executable, all_sessions=True
                ):
                    raise ProcessCleanupUnconfirmed(
                        "安装目录仍有进程运行，请关闭奇想盒后重启 MAS"
                    )
            return
        for executable in executables:
            try:
                if not await process_tools.kill_process(executable):
                    stopped = False
            except Exception as error:
                stopped = False
                logger.opt(exception=True).warning(f"中止奇想盒进程失败: {error}")
        try:
            await manager.kill()
        except Exception as error:
            logger.opt(exception=True).warning(f"清理奇想盒进程句柄失败: {error}")
        for executable in executables:
            if not await process_tools.is_process_stopped(executable):
                stopped = False
        if not stopped:
            raise ProcessCleanupUnconfirmed(
                "无法确认奇想盒进程已退出，已保留安装现场，请关闭奇想盒并重启 MAS"
            )
    except BaseException:
        script_install.retain_installation_until_restart(root)
        raise


def find_upstream_pids(root_path: Path) -> list[int]:
    """按安装根探测正在运行的上游进程（app 壳 + 后端 python），同步全扫。

    后端 python 以 exe 路径精确匹配（Path 比较对齐 ProcessManager 的
    match_process 惯例，嵌入式 python 与 MAS 自身解释器不同路径，不误伤）；
    app 壳按进程名匹配。属操作系统面的进程管理（MAS 领域），不读取上游
    内部状态。
    """

    pids: list[int] = []
    python_exe = Path(root_path) / REL_PYTHON_EXE
    for process in psutil.process_iter(["name", "exe"]):
        try:
            if process.info["name"] == APP_EXE_NAME and (
                not process.info["exe"]
                or Path(process.info["exe"]) == root_path / APP_EXE_NAME
            ):
                pids.append(process.pid)
                continue
            exe = process.info["exe"]
            if exe and Path(exe) == python_exe:
                pids.append(process.pid)
        except psutil.Error:
            continue
    return pids


class EmbeddedPythonLauncher:
    """IProcessLauncher 默认实现：复刻 app 的后端拉起方式。"""

    def __init__(self, root_path: Path, use_admin: bool) -> None:
        self.root_path = Path(root_path)
        self.use_admin = use_admin
        self.python_exe = self.root_path / REL_PYTHON_EXE
        self.process_manager = ProcessManager()
        # 按 exe 路径精确跟踪（python.exe 同名进程多，不能只按名字匹配）
        self.target_process = ProcessInfo(
            name="python.exe", exe=str(self.python_exe), cmdline=None
        )
        self.started = False
        self._launch_attempted = False

    def _build_env(self) -> dict[str, str]:
        """复刻 app 的 buildEnv：PATH 前插 python-embedded + PYTHONHOME。"""

        env = dict(os.environ)
        python_dir = str(self.python_exe.parent)
        parts = env.get("PATH", "").split(os.pathsep)
        if python_dir not in parts:
            env["PATH"] = python_dir + os.pathsep + env.get("PATH", "")
        env["PYTHONHOME"] = python_dir
        return env

    async def spawn(self) -> None:
        """拉起无头一条龙（cwd=安装根，configs/logs 相对 cwd 解析）。"""

        # 仅当 MAS 自身未提权时才走 runas 触发 UAC；已提权时子进程自动继承，
        # 并可带 env（ShellExecute 分支不支持 env，见模块注释）
        need_uac = self.use_admin and not IS_ELEVATED
        self._launch_attempted = True
        await launch_installation_process(
            self.process_manager,
            self.root_path,
            self.python_exe,
            *WHIMBOX_CLI_ARGS,
            target_process=None if in_desktop_session() else self.target_process,
            elevated=need_uac,
            env=None if need_uac else self._build_env(),
        )
        self.started = True
        logger.info(
            f"已启动奇想盒无头一条龙: {self.python_exe} "
            f"{' '.join(WHIMBOX_CLI_ARGS)} (cwd={self.root_path}, elevated={need_uac})"
        )

    async def is_running(self) -> bool:
        """无头进程是否仍在运行。"""

        if not self.started:
            return False
        return await self.process_manager.is_running()

    async def terminate(self) -> None:
        """只有确认进程停止后，调用方才可恢复配置或重试。"""
        if self._launch_attempted:
            await stop_installation_processes(
                self.process_manager, self.root_path, self.python_exe
            )
            self.started = False
            self._launch_attempted = False
