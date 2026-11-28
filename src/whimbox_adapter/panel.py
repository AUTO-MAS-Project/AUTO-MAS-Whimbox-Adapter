"""Provide installation metadata only when a Whimbox editor requests its block."""

import asyncio
from dataclasses import asdict
from pathlib import Path

from .api import WhimboxTaskCatalogData
from .config import WhimboxConfig, WhimboxUserConfig
from .tools.upstream import WheelAssetsConfigSurface


def _catalog(root: Path) -> dict:
    surface = WheelAssetsConfigSurface(root)
    if problem := surface.check_install():
        return {"error": problem}
    try:
        return WhimboxTaskCatalogData.model_validate(
            asdict(surface.read_catalog())
        ).model_dump(mode="json")
    except (OSError, ValueError, RuntimeError) as error:
        return {"error": str(error)}


async def read_panel_data(entry, page_id: str) -> dict:
    if page_id == "installation" and isinstance(entry, WhimboxConfig):
        root = entry.Info.RootPath
        if not root:
            return {}
        version = await asyncio.to_thread(
            WheelAssetsConfigSurface(Path(root)).upstream_version
        )
        return {"version": version}
    if page_id == "tasks" and isinstance(entry, WhimboxUserConfig):
        script = entry.parent.parent
        root = script.Info.RootPath
        return (
            await asyncio.to_thread(_catalog, Path(root))
            if root
            else {"errorKey": "panel.missingRoot"}
        )
    raise ValueError("未知的奇想盒编辑面板")
