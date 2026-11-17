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

"""奇想盒自动代理：无头 CLI ``startOneDragon`` + loguru 日志面判定。

执行链（对齐 BetterGI 线）：
1. ``check()``：安装哨兵三件 + 上游进程占用 + 今日次数/剩余天数；
2. 运行前：强制归档 ``configs/config.json`` → 若接管配置，把账号覆盖集按当前
   模板键集过滤后原子合并写入 → spawn 嵌入式 Python 无头一条龙；
3. 运行中：``IRunEventFeed`` 盯当日日志，实时把步骤/账号进展推调度台，
   判定交给 ``IRunResultParser``（管理员致命 / 收尾行 + 完成行结果块头 /
   中止 / 早退 / 停滞超时）；
4. 结束（final_task / on_crash 全覆盖）：停监控、清进程、还原运行前归档、
   写历史、落账号状态、推通知（结果块原文透传 + 未完成原因原文）。

成败判定全部取自上游结果面（日志收尾行与结果块），不自造判定信号；
多账号循环归上游 ``change_account``，MAS 不做账号定向。重试也只救「没有上游结论」
的异常（提前退出/卡死），上游连着给出同一个失败结论时收口，不重跑整条一条龙。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from auto_mas_core import AutoProxyWorker
from auto_mas_core.host.core import Notify
from auto_mas_core.host.core_history import aggregate_data
from auto_mas_core.host.models_notification import LocalNotice
from auto_mas_core.host.models_schema import HistoryAggregationItem, HistoryWrite
from auto_mas_core.host.models_task import LogRecord
from auto_mas_core.host.services import execute_script_task
from auto_mas_core.host.task_proxy_helpers import (
    CONFIG_BASE_NATIVE,
    push_dispatch_log,
    read_config_base,
)
from auto_mas_core.host.utils import UTC4, get_logger

import whimbox_adapter.notifications as _format_whimbox
from whimbox_adapter.config import WhimboxUserConfig

from .contracts import (
    IRunEventFeed,
    IRunResultParser,
    WhimboxRunEvent,
)
from .tools.feed import LoguruFileFeed, build_log_path_resolver, parse_log_timestamp
from .tools.launcher import EmbeddedPythonLauncher, find_upstream_pids
from .tools.marker import WhimboxMarkerParser, is_benign_marker
from .tools.update import RECOVERY_HINT, pending_marker
from .tools.upstream import WheelAssetsConfigSurface, parse_override_map

logger = get_logger("奇想盒 自动代理")

# 实时进展推送：值得转述到调度台的行前缀（步骤进入/异常/账号切换）
_DISPATCH_LINE_PREFIXES = (
    "✅",
    "❌",
    "🛑",
    "账号列表:",
    "当前账号:",
    "已完成账号列表：",
)

# 失败提示行前缀：结果块里默认步骤只有 ✅/⏭️/❌ 三态、不带原因，原因只在上游这些
# 行里，收集后进报告「未完成原因」段（原文透传，不改写成步骤级归属）
_FAILURE_LINE_PREFIXES = ("❌", "🛑")

# 不做自动重试的判定状态：成功（调用方在此之前已收口）与环境性致命（缺管理员权限等，
# 重试必然同一结果）
_NO_RETRY_STATUSES = frozenset({"success", "fatal"})


def _should_retry(
    verdict_status: str | None, *, same_reason_as_last: bool = False
) -> bool:
    """是否值得再跑一轮（``Run.RunTimesLimit`` 的重试策略）。

    - **没有上游结论**的形态（``exited_early`` 提前退出 / ``stalled`` 卡死 / 未判定）：
      重试——这才是重试机制的原本用途；
    - **上游自己判负**（``failed``）：只在**原因与上一轮不同**时重试（情形在变，说明
      还有救）；连着给出同一个结论时收口，因为重试等于把整条一条龙再跑一遍（含重新
      启动游戏），而原因不会在几分钟内自己变；
    - **环境性致命**（``fatal``，如缺管理员权限）：不重试，权限不会因重试而出现；
    - ``success``：不重试（调用方在此之前已收口）。
    """

    if verdict_status in _NO_RETRY_STATUSES:
        return False
    if verdict_status == "failed":
        return not same_reason_as_last
    return True


class AutoProxyTask(AutoProxyWorker):
    """奇想盒自动代理任务（每账号一轮）。

    四边界经构造注入，默认实现就地兜底构造（L1 组合，零容器）；单测经构造
    参数注入假实现，不经过 manager。
    """

    def __init__(
        self,
        expander,
        user,
        config,
        *,
        config_surface=None,
        launcher=None,
        parser=None,
        feed_factory=None,
    ):
        """接收 L3 已锁定的账号，建立本次执行拥有的进程、日志和恢复状态。"""
        super().__init__(expander, user, config)
        if not isinstance(config, WhimboxUserConfig):
            raise TypeError("奇想盒执行器需要已锁定的奇想盒账号配置")
        self.script_config = expander.script_config
        self.history = expander.history
        self.cur_user_item = user
        self.cur_user_config = config
        self.cur_user_uid = config.uid
        self._user_scripts_started = False
        self._log_progress = None
        self._log_append_lock = asyncio.Lock()
        self._started_run = False
        # base 来源三态（共享/独立/原生）：shared/independent=把面板值写入上游 config.json
        # （运行时物化）；原生来源=用上游原生配置、MAS 零写入——除非开启覆写层
        # （覆写常规配置，overlay：任务前物化面板覆盖集、结束还原，与家族「原生+开启」语义一致）
        self.config_base = read_config_base(self.cur_user_config)
        self.use_mas_config = self.config_base != CONFIG_BASE_NATIVE or bool(
            self.cur_user_config.Info.IfQuickConfig
        )

        self.root_path = Path(str(self.script_config.Info.RootPath or ""))
        self.config_surface = config_surface or WheelAssetsConfigSurface(self.root_path)
        self.launcher = launcher or EmbeddedPythonLauncher(
            self.root_path, bool(self.script_config.Run.UseAdmin)
        )
        # 只有默认实现启用增量扩展；注入判定器保持原 evaluate 全文契约。
        self._incremental_parser: WhimboxMarkerParser | None = None
        if parser is None:
            self._incremental_parser = WhimboxMarkerParser()
            self.parser: IRunResultParser = self._incremental_parser
        else:
            self.parser = parser
        self._feed_factory = feed_factory or (
            lambda on_event: LoguruFileFeed(
                on_event, build_log_path_resolver(self.root_path)
            )
        )
        self.feed: IRunEventFeed | None = None

        self.cur_user_log: LogRecord | None = None
        self.wait_event: asyncio.Event | None = None
        self.run_book = False
        self.result_block: str | None = None
        # 上游 ❌/🛑 提示行原文（结果块只有状态、没有原因），进报告「未完成原因」段
        self.failure_notes: list[str] = []
        self._failure_notes_seen: set[str] = set()
        # 本轮判定器的状态（_should_retry 输入）；每轮尝试开头清空
        self.last_verdict_status: str | None = None
        # 上一轮失败的判定文案（含上游原因），用于「同一原因不重复重试」
        self._last_failure_reason: str | None = None
        # 运行前归档锚点（None=运行前无 config.json，还原时删除会话期新建）
        self._pre_run_archive_ts: str | None = None
        # 本轮是否接管了上游配置（原生来源+关闭时不物化也不还原，上游自行迁移）
        self._config_took_over = False
        # 实时进展已推送的行数（避免重复转述）
        self._dispatch_pushed_lines = 0
        self._log_consumed_lines: int = 0
        self._use_incremental_log: bool = False
        # 进程静默退出看门狗（无日志行时事件流不会回调，靠轮询兜底）
        self._exit_watchdog_task: asyncio.Task | None = None

    async def _check_run(self) -> str:
        if pending_marker(self.root_path).exists():
            return RECOVERY_HINT
        # 安装哨兵三件（分场景报错，覆盖首启未完成/弱网下载失败场景）
        install_error = self.config_surface.check_install()
        if install_error:
            return install_error

        # 上游正在运行：拒绝启动（不静默互踩 config.json，MAA issue-591 教训）
        running_pids = await asyncio.get_running_loop().run_in_executor(
            None, find_upstream_pids, self.root_path
        )
        if running_pids:
            return "奇想盒正在运行，请先关闭奇想盒 app 再执行任务"

        # 启动检查早于每日重置，旧日期的次数不参与今日上限判断。
        curdate = datetime.now(tz=UTC4).strftime("%Y-%m-%d")
        proxy_times = (
            self.cur_user_config.Data.ProxyTimes
            if self.cur_user_config.Data.LastProxyDate.strftime("%Y-%m-%d") == curdate
            else 0
        )
        if (
            self.script_config.Run.ProxyTimesLimit != 0
            and proxy_times >= self.script_config.Run.ProxyTimesLimit
        ):
            await self.cur_user_item.set_state(status="跳过")
            return "今日代理次数已达上限, 跳过该账号"
        if self.cur_user_config.Info.RemainedDay == 0:
            await self.cur_user_item.set_state(status="跳过")
            return "账号剩余天数为 0, 跳过该账号"

        return "Pass"

    async def prepare(self):
        self.wait_event = asyncio.Event()
        self.user_start_time = datetime.now()

    def _takeover_config(self) -> None:
        """快照 → 物化：接管配置时把账号覆盖集写入上游 config.json。

        归档无条件执行（运行前安全网）；物化与还原只在接管时——原生态未开
        覆写层时 MAS 零写入，上游运行期的自我迁移（合并默认键等）属其内部
        行为，不予还原；原生态开启覆写层则任务前物化、结束还原（overlay）。
        """

        # 运行前强制归档（覆盖性物化前；失败即任务失败，不留无快照的覆写）
        self._pre_run_archive_ts = self.config_surface.snapshot_pre_run()
        self._config_took_over = self.use_mas_config
        if not self.use_mas_config:
            return
        tasks = parse_override_map(self.cur_user_config.Task.Tasks)
        options = parse_override_map(self.cur_user_config.Task.Options)
        self.config_surface.materialize_overrides(
            {str(k): bool(v) for k, v in tasks.items()},
            options,
            run_all_accounts=bool(self.cur_user_config.OneDragon.IfRunAllAccounts),
        )

    def _restore_config(self) -> None:
        """还原运行前归档条目（final_task / on_crash 共用，幂等）。"""

        if self._config_took_over:
            try:
                self.config_surface.restore_pre_run(self._pre_run_archive_ts)
            except Exception as e:
                logger.opt(exception=True).warning(f"还原奇想盒 config.json 失败: {e}")
        self._pre_run_archive_ts = None
        self._config_took_over = False

    async def main_logic(self) -> None:
        """在账号锁内检查准入并执行整轮代理，超时仍走同一恢复收尾。"""
        reason = await self._check_run()
        if reason != "Pass":
            if self.user_info.info.status == "跳过":
                return
            raise ValueError(reason)
        self._started_run = True
        try:
            async with asyncio.timeout(self.script_config.Run.HardTimeLimit * 60):
                await self._business_main()
        except TimeoutError:
            if self.cur_user_log is not None:
                await self.cur_user_log.set_state(status="奇想盒运行超过总时限，已终止")
            raise

    async def _business_main(self):
        self.curdate = datetime.now(tz=UTC4).strftime("%Y-%m-%d")
        if self.cur_user_config.Data.LastProxyDate.strftime("%Y-%m-%d") != self.curdate:
            self.cur_user_config.Data.LastProxyDate = datetime.now(tz=UTC4)
            await self.cur_user_config.commit()
            self.cur_user_config.Data.ProxyTimes = 0
            await self.cur_user_config.commit()

        await self.cur_user_item.set_state(status="运行")

        self._takeover_config()
        # 任务前脚本每账号一次，先于全部重试；后置脚本由收尾阶段成对执行
        await self.run_user_scripts_before()

        run_limit = int(self.script_config.Run.RunTimesLimit)
        for i in range(run_limit):
            if self.run_book:
                break
            logger.info(
                f"账号 {self.cur_user_item.info.name} - 尝试次数: {i + 1}/{run_limit}"
            )
            await self.cur_user_item.set_state(status="运行")
            self.last_verdict_status = None
            log_start_time = datetime.now().astimezone()
            self.cur_user_log = await self.add_log(log_start_time)
            await self.script_info.set_log("")
            self._dispatch_pushed_lines = 0
            self._log_consumed_lines = 0
            self._use_incremental_log = False
            if self._incremental_parser is not None:
                self._incremental_parser.reset()

            await push_dispatch_log(
                self.script_info, "启动奇想盒无头一条龙: startOneDragon"
            )
            self.wait_event.clear()
            self.feed = self._feed_factory(self._on_run_event)
            await self.launcher.spawn()
            # 启动稍作等待再盯日志：文件 sink 在进程初始化后才创建
            await asyncio.sleep(1)
            await self.feed.start(log_start_time)
            self._exit_watchdog_task = asyncio.create_task(self._exit_watchdog())

            await self.wait_event.wait()
            # 本轮收口：停看门狗与日志监控，再进入结果处理
            await self._stop_monitoring()

            if self.cur_user_log.info.status == "Success!":
                self.run_book = True
                await self.script_info.set_log(
                    "检测到奇想盒已完成一条龙序列\n正在等待奇想盒自行退出"
                )
                await asyncio.sleep(3)
                break

            logger.warning(
                f"账号 {self.cur_user_item.info.name} - 奇想盒代理异常: {self.cur_user_log.info.status}"
            )
            await self.script_info.set_log(
                f"{self.cur_user_log.info.status}\n正在中止相关程序"
            )
            await self.launcher.terminate()
            try:
                await Notify.send_notice(
                    LocalNotice(
                        title="奇想盒自动代理出现异常！",
                        message=f"账号 {self.cur_user_item.info.name} 的自动代理出现一次异常",
                        ticker=f"{self.cur_user_item.info.name}的自动代理出现异常",
                        duration_seconds=3,
                    )
                )
            except Exception:
                pass

            # 重试策略见 _should_retry：上游连着给出同一个结论时收口，不把整条
            # 一条龙再跑一遍（含重启游戏）；用户按提示处理后可手动重新运行
            reason = self.cur_user_log.info.status
            same_reason = reason != "" and reason == self._last_failure_reason
            self._last_failure_reason = reason
            if not _should_retry(
                self.last_verdict_status, same_reason_as_last=same_reason
            ):
                await self.script_info.append_log(
                    f"\n{reason}\n不再自动重试（同一失败原因已连续出现）"
                    "\n请按提示处理后重新运行"
                )
                logger.warning(
                    f"账号 {self.cur_user_item.info.name} - 判定 {self.last_verdict_status}"
                    f"（{reason}），跳过自动重试"
                )
                break

            if i + 1 < run_limit:
                await self.script_info.append_log(
                    f"\n将在稍后重试 ({i + 1}/{run_limit})"
                )
                await asyncio.sleep(10)

    async def _exit_watchdog(self) -> None:
        """无日志行时的兜底收口：进程退出与停滞超时都由轮询推进。

        feed 只在有新行时才派发事件，因此两件事必须在这里做：① 进程已退出时
        再等数秒让 push 队列里最后的日志行（含收尾行）先进回调，再判定；
        ② 进程仍在运行但日志静默时照常判定一次——停滞（``Run.RunTimeLimit``）
        只有这条路径可达，否则卡死的任务会永久挂起。
        """

        with suppress(asyncio.CancelledError):
            while self.wait_event is not None and not self.wait_event.is_set():
                await asyncio.sleep(2)
                if not await self.launcher.is_running():
                    # enqueue 落盘为毫秒级，此处等收尾行进回调后再收口
                    await asyncio.sleep(5)
                    await self._evaluate_and_release(
                        process_running=await self.launcher.is_running(),
                        refresh_pending=True,
                    )
                    return
                if self._run_event_context_ready():
                    assert self.feed is not None
                    time_limit = self.script_config.Run.RunTimeLimit
                    if self.is_log_stalled(self.feed.latest_time, minutes=time_limit):
                        await self._evaluate_and_release(refresh_pending=True)

    def _run_event_context_ready(self) -> bool:
        """本轮判定上下文是否就绪（feed/日志记录/等待事件均存在）。"""

        return (
            self.feed is not None
            and self.cur_user_log is not None
            and self.wait_event is not None
            and not self.wait_event.is_set()
        )

    async def _on_run_event(self, event: WhimboxRunEvent) -> None:
        """事件流回调：维护累计日志与实时进展推送，并按判定器收口。"""

        if not self._run_event_context_ready():
            return

        assert self.feed is not None and self.cur_user_log is not None

        self._use_incremental_log = event.log_delta is not None
        if event.log_delta is None:
            # 旧 feed 在首个事件前已暴露整批历史，保留其预读和全文判定契约。
            log = self.feed.log_text
            lines = log.splitlines(keepends=True)
            await self.cur_user_log.set_state(content=lines)
            self._collect_failure_notes(lines, self.feed.latest_time)
            if isinstance(self.feed, LoguruFileFeed):
                self._log_consumed_lines = self.feed.revision
            if self._incremental_parser is not None:
                # 快照与增量切换时，marker 缓存也须对应完整快照。
                self._incremental_parser.reset()
                self._incremental_parser.feed(log)
        else:
            # 先确认历史消费，再推实时进展；推送抛错不会重复历史和 marker。
            await self._append_log_delta(event.log_delta, log_offset=event.log_offset)

        await self.script_info.set_log("".join(self.cur_user_log.info.content))

        # 实时进展：步骤进入/异常/账号切换行的消息段转述到调度台（只推一次）。
        # loguru 行 = 「时间 | 级别 | 模块 - 消息」，emoji 前缀在消息段而非行首；
        # 结果块续行没有前缀结构，整行即消息。
        lines = self.cur_user_log.info.content
        for line in lines[self._dispatch_pushed_lines :]:
            message = (
                line.split(" - ", 1)[-1].strip() if " - " in line else line.strip()
            )
            if message.startswith(_DISPATCH_LINE_PREFIXES):
                await push_dispatch_log(self.script_info, message)
        self._dispatch_pushed_lines = len(lines)

        # 失败提示：上游结果块里默认步骤只有 ✅/⏭️/❌ 三态、不带原因，原因只在它自己的
        # ❌/🛑 行里——原样收集进报告（不把行映射回步骤：那是上游私有语义）。
        # 判据是「本行带时间列」（event.timestamp 非空即独立记录）：结果块的续行没有
        # 时间列，否则那几行 `❌…未完成` 会被当成失败提示重复收进来。
        # 「非失败提示」表（如「正在挖掘，无法收获」= 今日没得收）不进原因段，见
        # marker.py 的表注释；上游结果块原文本身不改写。
        if event.timestamp is not None and not isinstance(self.feed, LoguruFileFeed):
            self._add_failure_note(event.text)

        await self._evaluate_and_release()

    def _add_failure_note(
        self, text: str, *, timestamp_reference: datetime | None = None
    ) -> None:
        note = text.split(" - ", 1)[-1].strip()
        if (
            note.startswith(_FAILURE_LINE_PREFIXES)
            and not is_benign_marker(note)
            and note not in self._failure_notes_seen
            and (
                timestamp_reference is None
                or parse_log_timestamp(text, timestamp_reference) is not None
            )
        ):
            self.failure_notes.append(note)
            self._failure_notes_seen.add(note)

    def _collect_failure_notes(self, lines: list[str], latest_time: datetime) -> None:
        """整批终态判定前收齐带时间列的原因，不把结果块续行当原因。"""
        for line in lines:
            # 文件监控可能先读到末行的一部分，后续增量会把它拼回完整行。
            if line.endswith(("\n", "\r")):
                self._add_failure_note(line, timestamp_reference=latest_time)

    async def _append_log_delta(
        self, delta: tuple[str, ...], *, log_offset: int
    ) -> None:
        """追加尚未消费的完整日志行，同时维护历史和 marker 缓存。"""

        assert self.cur_user_log is not None and self.feed is not None
        # feed 与退出看门狗都可补消费；提交让出期间共享同一游标和 marker。
        async with self._log_append_lock:
            pending = delta[max(0, self._log_consumed_lines - log_offset) :]
            if not pending:
                return
            raw_text = "".join(pending)
            text = raw_text
            content = list(self.cur_user_log.info.content)
            # readlines 可返回未换行末段，只拼接该段，保留全文 splitlines 语义。
            if content and not content[-1].endswith(
                (
                    "\n",
                    "\r",
                    "\v",
                    "\f",
                    "\x1c",
                    "\x1d",
                    "\x1e",
                    "\x85",
                    "\u2028",
                    "\u2029",
                )
            ):
                text = content.pop() + text
            lines = text.splitlines(keepends=True)
            content.extend(lines)
            await self.cur_user_log.set_state(content=content)
            self._collect_failure_notes(lines, self.feed.latest_time)
            self._use_incremental_log = True
            if self._incremental_parser is not None:
                self._incremental_parser.feed(raw_text)
            self._log_consumed_lines = max(self._log_consumed_lines, log_offset) + len(
                pending
            )

    async def _evaluate_and_release(
        self, *, process_running: bool = True, refresh_pending: bool = False
    ) -> None:
        """按判定器收口本轮：致命/完成/中止/早退/停滞任一命中即释放等待。

        默认判定器只在收尾时物化全文（成败仍取自上游结果面）；停滞用单调时钟
        判定。提前退出仅由看门狗在落盘等待后传入，事件回调不抢先判早退。
        """

        if not self._run_event_context_ready():
            return

        assert self.feed is not None and self.cur_user_log is not None

        feed = self.feed
        # 回调失败时，静默重发可能晚于看门狗；终态前只补消费已采集的未处理行。
        if refresh_pending and isinstance(feed, LoguruFileFeed):
            delta = feed.get_log_delta(self._log_consumed_lines)
            if delta:
                await self._append_log_delta(delta, log_offset=self._log_consumed_lines)
        time_limit = self.script_config.Run.RunTimeLimit
        stalled = self.is_log_stalled(feed.latest_time, minutes=time_limit)
        if self._incremental_parser is not None and self._use_incremental_log:
            verdict = self._incremental_parser.evaluate_incremental(
                lambda: feed.log_text,
                process_running=process_running,
                stalled_minutes=time_limit,
                stalled=stalled,
            )
        else:
            # 保留构造注入的旧判定器契约。
            verdict = self.parser.evaluate(
                feed.log_text,
                process_running=process_running,
                stalled_minutes=time_limit,
                stalled=stalled,
            )

        if verdict.status == "running":
            await self.cur_user_log.set_state(status="奇想盒正常运行中")
            return

        self.last_verdict_status = verdict.status
        if verdict.status == "success":
            await self.cur_user_log.set_state(status="Success!")
            await self.cur_user_item.set_state(status="完成")
            self.result_block = verdict.result_block
        else:
            await self.cur_user_log.set_state(status=verdict.message)
            await self.cur_user_item.set_state(status="异常")

        logger.info(f"奇想盒任务结果: {self.cur_user_log.info.status}, 释放日志等待")
        self.wait_event.set()

    async def _stop_monitoring(self) -> None:
        """停看门狗与日志监控（final_task / on_crash 共用，每步独立容错）。"""

        try:
            if self._exit_watchdog_task is not None:
                watchdog = self._exit_watchdog_task
                self._exit_watchdog_task = None
                watchdog.cancel()
                with suppress(asyncio.CancelledError):
                    await watchdog
        finally:
            if self.feed is not None:
                with suppress(Exception):
                    await self.feed.stop()

    async def finish(self):
        if not self._started_run:
            return
        self.history.begin_user_history_capture(
            self.script_info,
            self.cur_user_item,
            failed=bool(self.cur_user_item.logs)
            and not self.run_book
            and not self.stopped_manually,
        )

        stopped = False
        try:
            # 先停止日志写入和进程并恢复原配置，结果提交失败也不遗留覆盖层。
            try:
                await self._stop_monitoring()
            finally:
                await self.launcher.terminate()
                stopped = True
                self._restore_config()
            await self._persist_user_run_result()
        finally:
            if stopped:
                await self.run_user_scripts_after()

        history_records = []
        for t, log_item in self.iter_logs():
            if log_item.info.status == "奇想盒正常运行中" or (
                self.stopped_manually
                and log_item is self.cur_user_log
                and log_item.info.status == "未开始监看日志"
            ):
                await log_item.set_state(status="任务被用户手动中止")

            was_cancelled = (
                self.stopped_manually
                and log_item is self.cur_user_log
                and log_item.info.status == "任务被用户手动中止"
            )
            if len(log_item.info.content) == 0:
                await log_item.set_state(content=["未捕获到任何日志内容"])
                if not was_cancelled:
                    await log_item.set_state(status="未捕获到日志")

            history_records.append(
                HistoryWrite(
                    time=t.astimezone(UTC4),
                    username=self.cur_user_item.info.name,
                    script_name=self.script_info.info.name,
                    status=(
                        "success"
                        if log_item.info.status == "Success!"
                        else "cancelled"
                        if was_cancelled
                        else "error"
                    ),
                    message=log_item.info.status,
                    logs="".join(log_item.info.content),
                )
            )

        if history_records:
            await self.history.save_user_history(
                history_records,
                script=self.script_info,
                user=self.cur_user_item,
                log_warning=logger.opt(exception=True).warning,
            )

        if history_records:
            try:
                statistics = {
                    key: value.value
                    for key, value in aggregate_data(
                        [
                            HistoryAggregationItem(time=record.time, data=record.data)
                            for record in history_records
                        ]
                    ).data.items()
                }
                if self.result_block:
                    # 上游「任务结果如下」块原样透传（多账号天然按「账号N：」分组）
                    statistics["result_block"] = self.result_block
                if self.failure_notes:
                    # 结果块默认步骤只有三态、不带原因：附上上游的 ❌/🛑 提示行原文
                    statistics["failure_notes"] = self.failure_notes
                statistics["user_info"] = self.cur_user_item.info.name
                start_time = getattr(self, "user_start_time", datetime.now())
                statistics["start_time"] = start_time.strftime("%Y-%m-%d %H:%M:%S")
                statistics["end_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                statistics["user_result"] = (
                    "一条龙任务已完成" if self.run_book else self.cur_user_item.result
                )
                success_symbol = "√" if self.run_book else "X"
                await Notify.push_statistics_report(
                    payload=_format_whimbox.build_statistics_payload(
                        title=f"{datetime.now().strftime('%m-%d')} |{success_symbol}|  {self.cur_user_item.info.name} 的奇想盒自动代理统计报告",
                        message=statistics,
                    ),
                    user_config=self.cur_user_config,
                    task_info=self.task_info,
                    compact_summary=True,
                )
            except Exception as e:
                # 失败不再静默：既记 ERROR 日志，也推到调度台实时日志
                await push_dispatch_log(self.script_info, f"推送账号统计通知失败: {e}")
                logger.opt(exception=True).error(
                    f"推送奇想盒账号统计通知时出现异常: {e}"
                )

    async def _persist_user_run_result(self) -> None:
        if self.cur_user_config is None:
            return

        if self.run_book:
            if (
                self.cur_user_config.Data.ProxyTimes == 0
                and self.cur_user_config.Info.RemainedDay != -1
            ):
                self.cur_user_config.Info.RemainedDay = (
                    self.cur_user_config.Info.RemainedDay - 1
                )
                await self.cur_user_config.commit()
            self.cur_user_config.Data.ProxyTimes = (
                self.cur_user_config.Data.ProxyTimes + 1
            )
            await self.cur_user_config.commit()
            self.cur_user_config.Data.LastProxyStatus = "成功"
            await self.cur_user_config.commit()
            await self.cur_user_item.set_state(status="完成")
            logger.success(f"账号 {self.cur_user_uid} 的奇想盒自动代理任务已完成")
        else:
            self.cur_user_config.Data.LastProxyStatus = "失败"
            await self.cur_user_config.commit()
            if self.cur_user_item.info.status != "完成":
                await self.cur_user_item.set_state(status="异常")

    async def on_crash(self, exc: BaseException) -> None:
        """记录异常；进程、配置恢复和结果交付统一由 finish 收敛。"""
        if self.cur_user_log is not None:
            await self.cur_user_log.set_state(status=f"奇想盒运行异常: {exc}")
        if self.wait_event is not None:
            self.wait_event.set()
        await super().on_crash(exc)

    @property
    def stopped_manually(self):
        """区分主动中止与上游给出的执行结论。"""
        return self.cancelled or self.task_info.info.status == "cancelled"

    def iter_logs(self):
        """按运行树中的顺序读取当前账号日志，不重建账号节点。"""
        return [
            (item.info.start_time.astimezone(), item)
            for item in self.user_info.logs.values()
        ]

    async def add_log(self, moment):
        """登记本次尝试的实际日志节点。"""
        uid = self.user_info.logs.add(
            LogRecord, payload={"info": {"start_time": moment}}
        )
        await self.user_info.logs.commit()
        return self.user_info.logs[uid]

    def is_log_stalled(self, latest, minutes):
        """用单调时钟判断日志是否持续无进展。"""
        now = time.monotonic()
        previous, since = self._log_progress or (None, now)
        if latest != previous:
            since = now
        self._log_progress = (latest, since)
        return now - since > minutes * 60

    async def run_user_scripts_before(self):
        """账号前置脚本每轮只执行一次。"""
        self._user_scripts_started = True
        if self.cur_user_config.Info.IfScriptBeforeTask:
            await execute_script_task(
                self.cur_user_config.Info.ScriptBeforeTask, "奇想盒账号任务前脚本"
            )

    async def run_user_scripts_after(self):
        """为已进入的账号前置阶段成对执行一次后置脚本。"""
        if not self._user_scripts_started:
            return
        self._user_scripts_started = False
        if self.cur_user_config.Info.IfScriptAfterTask:
            try:
                await execute_script_task(
                    self.cur_user_config.Info.ScriptAfterTask, "奇想盒账号任务后脚本"
                )
            except Exception as error:
                message = f"账号任务后脚本执行失败：{error}"
                logger.opt(exception=True).warning(message)
                logs = self.iter_logs()
                if logs:
                    await logs[-1][1].append_content(message + "\n")
