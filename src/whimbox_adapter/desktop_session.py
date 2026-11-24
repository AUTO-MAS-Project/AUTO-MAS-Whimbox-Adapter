"""同号 SDK 的能力差异：旧宿主保留设置，执行沿用主会话。"""

from typing import Literal

from auto_mas_core.adapters import script as script_sdk
from auto_mas_core.host.config import ConfigGroup
from pydantic import Field


class _LegacyRunFields(ConfigGroup):
    ExecutionSession: Literal["main", "desktop"] = Field(
        default="main", description="单独启动托管时使用的 Windows 会话"
    )


DesktopSessionRunFields = getattr(
    script_sdk, "DesktopSessionRunFields", _LegacyRunFields
)


def in_desktop_session() -> bool:
    check = getattr(script_sdk, "in_desktop_session", None)
    return check is not None and check()


def desktop_session_check(config, elevated: bool):
    return None if elevated else "admin_required"
