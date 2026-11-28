"""奇想盒适配声明，执行、配置与结果语义由插件持有。"""

from auto_mas_core import Config, ScriptAdapterPlugin, ScriptTypeDecl, TaskMode
from fastapi import APIRouter

from .api import router as task_router
from .backup_api import router as backup_router
from .config import WhimboxConfig, WhimboxUserConfig
from .desktop_session import desktop_session_check
from .manager import WhimboxManager, check_prefetch
from .tools.restore_service import RESTORE_POOLS

router = APIRouter()
router.include_router(backup_router)
router.include_router(task_router, prefix="/api/scripts")


class Plugin(ScriptAdapterPlugin):
    script_type_key = "Whimbox"
    api_router = router
    decl = ScriptTypeDecl(
        script_type=WhimboxConfig,
        user_type=WhimboxUserConfig,
        expander_class=WhimboxManager,
        supported_modes=(TaskMode.AUTO_PROXY, TaskMode.SCRIPT_CONFIG),
        prefetch_check=check_prefetch,
        **(
            {"desktop_session_check": desktop_session_check}
            if hasattr(ScriptTypeDecl, "desktop_session_check")
            else {}
        ),
        activity_game="infinitynikki",
        restore_pools=tuple(RESTORE_POOLS),
    )

    async def pre_enable(self):
        """在配置激活前接入宿主引用集合。"""
        refs = {"PlanConfig": Config.PlanConfig}
        WhimboxConfig.related_config = refs
        WhimboxUserConfig.related_config = refs
