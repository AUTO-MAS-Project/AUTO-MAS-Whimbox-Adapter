"""奇想盒托管与账号配置，保留已有配置身份与字段。"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from typing import Annotated, Literal

from auto_mas_core import ScriptEntry, UserEntry
from auto_mas_core.host.config import (
    ConfigCollection,
    ConfigGroup,
    Virtual,
    collection,
    legacy,
    tz,
    ui,
    virtual_field,
)
from auto_mas_core.host.config_types import (
    FilePath,
    FolderPath,
    JsonDictString,
    WindowsNameString,
)
from auto_mas_core.host.models_config_queue import Webhook
from auto_mas_core.host.models_config_shared import (
    _tag_notes,
    _tag_proxy,
    _tag_remained_days,
    none_as,
)
from auto_mas_core.host.utils import constants as _utils_constants
from pydantic import Field

from .desktop_session import DesktopSessionRunFields


class WhimboxUserConfig(UserEntry):
    """奇想盒账号配置（无限暖暖，一条龙配置档）

    一条龙字段定义与值域完全来自上游安装目录三件套（任务目录，运行时读取），
    本类只存「用户勾了哪些步骤、选了哪些值」的覆盖集（Tasks/Options 两个
    JSON map），不复制上游字段模型；写入时按当前模板键集过滤后物化到上游
    configs/config.json。
    """

    class DataFields(ConfigGroup):
        LastProxyDate: Annotated[
            datetime, tz(_utils_constants.UTC4), ui(format="date")
        ] = Field(
            description="上次代理日期",
            default_factory=lambda: datetime(2000, 1, 1, tzinfo=_utils_constants.UTC4),
        )

        LastProxyStatus: Literal["未知", "成功", "失败"] = Field(
            description="上次代理状态（未知/成功/失败）", default_factory=lambda: "未知"
        )

        ProxyTimes: Annotated[int, Field(ge=0, le=9999)] = Field(
            description="代理次数", default_factory=lambda: 0
        )

    Data: DataFields = Field(default_factory=DataFields)

    class InfoFields(ConfigGroup):
        Base: Annotated[
            Literal["shared", "independent", "native"],
            legacy(
                group="Info",
                name="Mode",
                convert=_utils_constants.normalize_base_source,
            ),
        ] = Field(
            default_factory=lambda: "shared",
            description="base 来源三态（shared=共享/independent=独立/native=原生）：共享/独立= 用 MAS 面板配置（当前运行行为一致，为后续特殊功能预留），原生=直接用奇想盒 自带配置（MAS 零写入）",
        )

        IfQuickConfig: bool = Field(
            default_factory=lambda: False,
            description="是否启用覆写层（覆写常规配置，与来源独立，按账号保存，默认关）：开启时原生态 也会在任务前把面板覆盖集写入奇想盒（overlay，任务结束还原）；共享/独立态 面板本就是 base 来源，开关暂无额外消费点，为后续特殊功能预留。默认关保证 「原生=零写入」与 _write_config_or_hint 报错里「切原生绕开写入」的出路成立",
        )

        IfScriptAfterTask: bool = Field(
            description="是否在任务后执行脚本", default_factory=lambda: False
        )

        IfScriptBeforeTask: bool = Field(
            description="是否在任务前执行脚本", default_factory=lambda: False
        )

        Name: WindowsNameString = Field(
            description="账号名称", default_factory=lambda: "新账号"
        )

        Notes: str = Field(description="备注", default_factory=lambda: "无")

        RemainedDay: Annotated[int, Field(ge=-1, le=9999), none_as(-1)] = Field(
            description="剩余天数", default_factory=lambda: -1
        )

        ScriptAfterTask: FilePath = Field(
            description="任务后脚本路径", default_factory=lambda: None
        )

        ScriptBeforeTask: FilePath = Field(
            description="任务前脚本路径", default_factory=lambda: None
        )

        Status: bool = Field(description="账号状态", default_factory=lambda: True)

        Tag: Virtual[str] = Field(
            description="账号标签列表（JSON字符串，TagItem的dict列表）",
            default_factory=lambda: None,
        )

    Info: InfoFields = Field(default_factory=InfoFields)

    class NotifyFields(ConfigGroup):
        Enabled: bool = Field(
            default_factory=lambda: False, description="是否启用账号通知"
        )

        IfSendMail: bool = Field(
            default_factory=lambda: False, description="是否发送邮件"
        )

        IfSendStatistic: bool = Field(
            default_factory=lambda: False, description="是否发送账号统计信息"
        )

        IfServerChan: bool = Field(
            default_factory=lambda: False, description="是否启用 Server 酱"
        )

        ServerChanKey: str = Field(
            default_factory=lambda: "", description="Server 酱密钥"
        )

        ToAddress: str = Field(
            default_factory=lambda: "", description="账号通知收件地址"
        )

    Notify: NotifyFields = Field(default_factory=NotifyFields)

    class OneDragonFields(ConfigGroup):
        IfRunAllAccounts: bool = Field(
            default_factory=lambda: False,
            description="一条龙是否循环全部游戏账号（物化为上游 OneDragon.change_account； 账号列表由奇想盒运行期 OCR 动态发现，MAS 不做账号定向）",
        )

    OneDragon: OneDragonFields = Field(default_factory=OneDragonFields)

    class TaskFields(ConfigGroup):
        Options: JsonDictString = Field(
            default_factory=lambda: "{ }",
            description="目标/参数覆盖集：JSON map {键: 值}，键集来自任务目录 （上游 OneDragon 节托管字段），只存用户显式选择的值",
        )

        Tasks: JsonDictString = Field(
            default_factory=lambda: "{ }",
            description="步骤开关覆盖集：JSON map {步骤键: bool}，键集来自任务目录 （上游 OneDragonDefaultSteps 节），只存用户勾选结果",
        )

    Task: TaskFields = Field(default_factory=TaskFields)
    Notify_CustomWebhooks: ConfigCollection[Webhook] = collection(Webhook)

    def getTags(self) -> str:
        """生成奇想盒账号标签列表"""
        tags = []
        tags.append(_tag_proxy(self, "任务"))
        tags.append(_tag_remained_days(self))
        tags.append(_tag_notes(self))
        return json.dumps(tags, ensure_ascii=False)

    @virtual_field("Info.Tag")
    def compute_Info_Tag(self):
        try:
            return self.getTags()
        except Exception as exc:
            return str(exc)

    class Metadata(UserEntry.Info):
        name: Virtual[str] = None  # pyright: ignore[reportIncompatibleVariableOverride]
        enabled: Virtual[bool] = None  # pyright: ignore[reportIncompatibleVariableOverride]

    info: Metadata = Field(default_factory=Metadata)  # pyright: ignore[reportIncompatibleVariableOverride]

    @virtual_field("info.name")
    def plugin_name(self):
        """向宿主提供已有账号名称。"""
        return self.Info.Name

    @virtual_field("info.enabled")
    def plugin_enabled(self):
        """向宿主提供已有账号启用状态。"""
        return self.Info.Status


class WhimboxConfig(ScriptEntry):
    "奇想盒配置（无限暖暖，BetterGI 线：无头 CLI + 日志面判定）"

    class InfoFields(ConfigGroup):
        Name: str = Field(
            description="托管名称", default_factory=lambda: "新奇想盒托管"
        )

        RootPath: FolderPath = Field(
            default_factory=lambda: None,
            description="奇想盒安装根目录（whimbox_app.exe 所在目录；configs/logs 与其同级）",
        )

    Info: InfoFields = Field(default_factory=InfoFields)

    class RunFields(DesktopSessionRunFields):
        HardTimeLimit: Annotated[int, Field(ge=1, le=9999), none_as(1)] = Field(
            default_factory=lambda: 120,
            description="单账号运行总时限（分钟），包含等待和全部重试",
        )

        ProxyTimesLimit: Annotated[int, Field(ge=0, le=9999), none_as(0)] = Field(
            description="每日代理次数上限（0=不限）",
            default_factory=lambda: 0,
        )

        RunTimeLimit: Annotated[int, Field(ge=1, le=9999), none_as(1)] = Field(
            default_factory=lambda: 30,
            description="运行超时（分钟）：日志静默超过该时长判定卡死",
        )

        RunTimesLimit: Annotated[int, Field(ge=1, le=9999), none_as(1)] = Field(
            default_factory=lambda: 3,
            description="失败重试次数：只用于「没有上游结论」的异常（进程提前退出/卡死）； 上游自己判负时，仅当失败原因与上一轮不同才继续重试（同一结论连着出现即收口）",
        )

        UseAdmin: bool = Field(
            default_factory=lambda: True,
            description="以管理员权限启动奇想盒后端（上游强制管理员，缺权限会在起跑即退出； MAS 自身已提权时不重复触发 UAC，子进程自动继承管理员令牌）",
        )

    Run: RunFields = Field(default_factory=RunFields)

    class UpdateFields(ConfigGroup):
        TakeoverEnabled: bool = Field(
            default_factory=lambda: False,
            description="运行前检查并安装奇想盒官方后端正式版，更新完成后启动任务；不更新 app",
        )

    Update: UpdateFields = Field(default_factory=UpdateFields)
    users: ConfigCollection[WhimboxUserConfig] = collection(WhimboxUserConfig)

    class Metadata(ScriptEntry.Info):
        name: Virtual[str] = None  # pyright: ignore[reportIncompatibleVariableOverride]

    info: Metadata = Field(default_factory=Metadata)  # pyright: ignore[reportIncompatibleVariableOverride]

    @virtual_field("info.name")
    def plugin_name(self):
        """向宿主提供已有托管名称。"""
        return self.Info.Name

    @classmethod
    def normalize_payload(cls, data):
        """迁移原账号集合名称，保留 UID、顺序及字段。"""
        data = deepcopy(data)
        if "UserData" in data:
            data["users"] = data.pop("UserData")
        if "UserData" in data.get("SubConfigsInfo", {}):
            data["SubConfigsInfo"]["users"] = data["SubConfigsInfo"].pop("UserData")
        return data

    async def _activate_from_payload(self, payload, *, regenerate_uuids=False):
        """在集合激活边界转换旧配置文档。"""
        await super()._activate_from_payload(
            type(self).normalize_payload(payload), regenerate_uuids=regenerate_uuids
        )

    @property
    def UserData(self):
        """供迁入业务使用同一账号集合。"""
        return self.users
