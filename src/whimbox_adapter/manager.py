"""奇想盒 L3：托管锁内准备安装，按固定账号清单派发插件执行器。"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from auto_mas_core import TaskMode, UserExpander
from auto_mas_core.host.config import config_manager
from auto_mas_core.host.core import Notify
from auto_mas_core.host.core_script_update import (
    ScriptUpdateRequest,
    auto_recover_script_install,
    run_managed_script_install,
    run_script_update_with_safe_fallback,
)
from auto_mas_core.host.core_script_update_recovery import (
    DownloadRetryFailed,
    InstallInspection,
)
from auto_mas_core.host.models_notification_report import TaskResultReport
from auto_mas_core.host.services import script_install
from auto_mas_core.host.services_common_update_prefetch import prefetch_paths
from auto_mas_core.host.services_script_install_backup import ManagedInstallBackup
from auto_mas_core.host.task_proxy_helpers import push_dispatch_log
from auto_mas_core.host.utils import get_logger
from auto_mas_core.host.utils_common_fluxdown_cli import FluxdownCancelUnconfirmed
from auto_mas_core.host.utils_common_process_runner import (
    is_process_cleanup_unconfirmed,
)

from .auto_proxy import AutoProxyTask
from .config_session import ScriptConfigTask
from .tools import update as backend_update
from .tools.managed_recovery import WhimboxInstallAdapter
from .tools.update import (
    RECOVERY_HINT,
    WhimboxUpdateUnsafeError,
    confirm_usable_installation,
    installation_access,
    pending_marker,
)
from .tools.upstream import APP_EXE_NAME, WheelAssetsConfigSurface

logger = get_logger("奇想盒 调度器")


async def check_prefetch(config):
    """复用统一下载中心准备需要更新的官方 wheel。"""
    from auto_mas_core.host.models_schema import DownloadRequest

    root = config.Info.RootPath
    if not config.Update.TakeoverEnabled or root is None:
        return []
    target, _name, url, digest = backend_update.select_wheel(
        await backend_update.request_release()
    )
    if (
        not pending_marker(root).exists()
        and await backend_update.installed_version(root) >= target
    ):
        return []
    return [DownloadRequest(url=url, content_hash=digest)]


class WhimboxManager(UserExpander):
    def __init__(self, script, config):
        """接收已锁托管；用户列表、用户锁与执行清理由标准 L3/L4 持有。"""
        super().__init__(script, config)
        self._resources = ExitStack()
        self.begin_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def can_run_user(self, entry):
        """沿用账号启用及剩余天数的代理准入条件。"""
        return super().can_run_user(entry) and (
            self.task_info.mode != TaskMode.AUTO_PROXY or entry.Info.RemainedDay != 0
        )

    async def check(self):
        """配置会话只要求原生 app 可启动，代理安装检查归实际执行器。"""
        root = self.script_config.Info.RootPath
        if root is None or not Path(root).is_dir():
            return "请先设置有效的奇想盒安装目录"
        if (
            self.task_info.mode == TaskMode.SCRIPT_CONFIG
            and not (Path(root) / APP_EXE_NAME).is_file()
        ):
            return "请先设置有效的奇想盒安装目录"
        return None

    async def prepare(self):
        """在托管锁内完成安装恢复与更新，资源访问保持到所有用户恢复结束。"""
        root = Path(self.script_config.Info.RootPath)
        self._resources.enter_context(installation_access(root))
        await WheelAssetsConfigSurface(root).restore_pending_overlay()
        if self.task_info.mode == TaskMode.AUTO_PROXY:
            token = prefetch_paths.set(self.context.params.get("prefetch_paths", {}))
            try:
                await self._prepare_update(root)
            finally:
                prefetch_paths.reset(token)

    async def _prepare_update(self, root):
        request = ScriptUpdateRequest(str(self.script_info.info.script_id), "Whimbox")
        marker = pending_marker(root)
        backup = ManagedInstallBackup(
            root, script_id=request.script_id, script_type="Whimbox"
        )
        recovered = False
        if backup.read() is not None:
            # 新公共事务优先：下一轮先验证是否成功，否则重试安装，
            # 必要时由公共层执行快照回滚与任务末尾汇总。
            adapter = WhimboxInstallAdapter(root)
            result = await run_managed_script_install(
                request,
                install_root=root,
                inspect=adapter.inspect,
                acknowledge=adapter.acknowledge,
                install=adapter.install,
                ensure_idle=adapter.ensure_idle,
                after_rollback=adapter.after_rollback,
            )
            await push_dispatch_log(self.script_info, f"奇想盒自动恢复：{result}")
            recovered = True
        elif marker.exists() or marker.is_symlink():
            # 兼容升级前留下的旧安装标记。这类记录没有旧版完整快照，
            # 只能强健康检查后确认或重新安装，不得伪造自动回滚。
            adapter = WhimboxInstallAdapter(root)
            result = await auto_recover_script_install(
                request,
                install_root=root,
                inspect=adapter.inspect,
                acknowledge=adapter.acknowledge,
                reinstall=adapter.install,
            )
            await push_dispatch_log(self.script_info, f"奇想盒上次安装恢复：{result}")
            recovered = True

        if self.script_config.Update.TakeoverEnabled and not recovered:
            await push_dispatch_log(self.script_info, "正在检查奇想盒后端更新…")

            async def prepare_without_installing() -> (
                backend_update.PreparedUpdate | str
            ):
                try:
                    return await backend_update.prepare_update(root)
                except (
                    WhimboxUpdateUnsafeError,
                    script_install.InstallUnsafeError,
                    FluxdownCancelUnconfirmed,
                ):
                    raise
                except Exception as error:
                    if is_process_cleanup_unconfirmed(error):
                        raise
                    # 准备阶段只写隔离下载缓存，不更改安装；由公共入口
                    # 检查旧版健康后决定是否继续并纳入队列最终报告。
                    raise DownloadRetryFailed(
                        source="GitHub/PyPI",
                        reason=f"奇想盒更新包准备失败（{type(error).__name__}）",
                    ) from error

            async def inspect_healthy_previous() -> InstallInspection:
                if backup.read() is not None:
                    raise script_install.InstallUnsafeError("仍有待处理的旧版备份")
                await confirm_usable_installation(root)
                version = await backend_update.installed_version(root)
                return InstallInspection(
                    pending=False,
                    healthy=True,
                    installed_version=str(version),
                    previous_confirmed=True,
                )

            candidate = await run_script_update_with_safe_fallback(
                request,
                prepare_without_installing,
                root,
                inspect_healthy_previous,
            )
            try:
                if isinstance(candidate, str):
                    # 包准备失败已在公共入口校验、汇报；正常无更新也须核验。
                    await confirm_usable_installation(root)
                    result = candidate
                else:
                    adapter = WhimboxInstallAdapter(
                        root,
                        target=candidate.target,
                        previous=candidate.current,
                        candidate=candidate,
                    )
                    result = await run_managed_script_install(
                        request,
                        install_root=root,
                        inspect=adapter.inspect,
                        acknowledge=adapter.acknowledge,
                        install=adapter.install,
                        ensure_idle=adapter.ensure_idle,
                        after_rollback=adapter.after_rollback,
                    )
            finally:
                # 备份建立失败与取消也要清理已下载包，保留未停写缓存
                # 的安全规则仍由 discard_update 统一控制。
                backend_update.discard_update(candidate)
            await push_dispatch_log(self.script_info, str(result))
        elif marker.exists() or marker.is_symlink():
            raise WhimboxUpdateUnsafeError(RECOVERY_HINT)

    def build_worker(self, user, config):
        """按插件声明的真实模式派发已锁用户。"""
        root = self.script_config.Info.RootPath
        script_install.assert_no_pending_process(root)
        if WheelAssetsConfigSurface(root).has_pending_overlay():
            raise script_install.InstallUnsafeError("奇想盒上次运行的原生配置尚未恢复")
        if self.task_info.mode == TaskMode.AUTO_PROXY:
            return AutoProxyTask(self, user, config)
        if self.task_info.mode == TaskMode.SCRIPT_CONFIG:
            return ScriptConfigTask(self, user, config)
        raise ValueError("奇想盒不支持该任务模式")

    async def finish(self):
        """用户收尾后保存配置和汇总结果，最后归还安装访问。"""
        try:
            await config_manager.flush()
            if self.task_info.mode == TaskMode.AUTO_PROXY:
                users = list(self.script_info.users.values())
                completed = sum(user.info.status == "完成" for user in users)
                data = {
                    "title": "自动代理任务报告",
                    "script_name": self.script_info.info.name or "空白",
                    "start_time": self.begin_time,
                    "end_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "completed_count": completed,
                    "uncompleted_count": sum(
                        user.info.status not in ("完成", "跳过") for user in users
                    ),
                    "result": self.script_info.result,
                }
                await Notify.push_proxy_result(
                    title=f"{self.script_info.info.name or '空白'}的自动代理任务报告",
                    report=TaskResultReport.from_message(data),
                    task_info=self.task_info,
                )
        finally:
            self._resources.close()
