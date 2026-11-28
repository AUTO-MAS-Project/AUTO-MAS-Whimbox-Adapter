"""奇想盒适配声明，执行、配置与结果语义由插件持有。"""

from dataclasses import replace

from auto_mas_core import Config, ScriptAdapterPlugin, ScriptTypeDecl, TaskMode
from auto_mas_core.host.config_types import FolderPath
from fastapi import APIRouter
from pydantic import TypeAdapter

from .api import router as task_router
from .backup_api import router as backup_router
from .config import HAS_PANEL_DATA, WhimboxConfig, WhimboxUserConfig
from .desktop_session import desktop_session_check
from .manager import WhimboxManager, check_prefetch
from .panel import read_panel_data
from .tools.restore_service import RESTORE_POOLS
from .tools.upstream import APP_EXE_NAME

_INSTALL_FOLDER = TypeAdapter(FolderPath)

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
        **(
            {
                "read_panel_data": read_panel_data,
                "restore_pools": tuple(
                    replace(pool, editor_scope="user") for pool in RESTORE_POOLS
                ),
            }
            if HAS_PANEL_DATA
            else {}
        ),
    )

    def validate_script_update(self, data):
        """选目录时检查原生入口，清空路径及加载已有配置沿用宿主规则。"""
        candidate = data.get("Info", {}).get("RootPath")
        if candidate is not None:
            root = _INSTALL_FOLDER.validate_python(candidate)
            if root is None or not (root / APP_EXE_NAME).is_file():
                raise ValueError(f"请选择含有 {APP_EXE_NAME} 的奇想盒安装目录")

    async def pre_enable(self):
        """在配置激活前接入宿主引用集合。"""
        refs = {"PlanConfig": Config.PlanConfig}
        WhimboxConfig.related_config = refs
        WhimboxUserConfig.related_config = refs
