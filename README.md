# AUTO-MAS Whimbox Adapter

AUTO-MAS 的奇想盒（无限暖暖）专项插件。

插件调用奇想盒官方 `startOneDragon` 入口，消费其日志结果；保留原生配置、MAS 账号覆盖、配置备份恢复、后端更新和通知能力。

## 本地开发

将仓库克隆到 AUTO-MAS 主仓的 `plugins/whimbox_adapter`。在主仓执行 `yarn dev`，然后在插件中心启用源码插件。前端目前随主程序构建，入口为主仓 `frontend/src/views/whimbox/entry.ts`；主程序尚不支持独立加载插件前端。

旧托管仍使用 `WhimboxConfig` / `WhimboxUserConfig` 类型及原有 UID。插件在激活时将 `UserData` 转为 `users`，不重建账号。禁用沿用宿主语义，会删除本插件的托管和账号；操作前应先备份。

账号覆盖写入前会保存原生配置。若配置恢复失败，解除文件占用后可重试，MAS 会先恢复原生配置再启动任务。若原生配置已有新修改，请在配置恢复中选择所需备份，恢复时会先存底。若 MAS 无法确认奇想盒进程已退出，请先关闭奇想盒再重启 MAS。

## 验证与构建

使用与本插件匹配的 AUTO-MAS 源码及开发依赖。设置 `AUTO_MAS_HOST_SOURCE` 为主仓绝对路径，然后执行：

```powershell
python -m pytest tests -q
python -m ruff check src tests
python -m ruff format --check src tests
uv build
```

本次迁移未改变上游游戏执行与结果判定语义。真实游戏、管理员授权和线上更新需在装有奇想盒的 Windows 环境验收。

## 发布

与 MAA Adapter 使用相同的 SDK 依赖范围 `>=5,<6`。运行需要包含奇想盒插件迁移的宿主。插件市场安装使用随宿主提供的 SDK 和版本元数据。独立使用 pip 安装时需要另行提供兼容的 SDK wheel。

分支推送和 PR 会构建并校验 wheel 与源码包。首次上传使用 PyPI [pending Trusted Publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)，配置如下。

| 字段 | 值 |
| --- | --- |
| PyPI project name | `auto-mas-whimbox-adapter` |
| Owner | `AUTO-MAS-Project` |
| Repository name | `AUTO-MAS-Whimbox-Adapter` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

将经验证的发布提交合入 `master` 后，推送与 `pyproject.toml` 版本一致的 `v<版本>` 标签。发布任务将构建校验通过的文件上传到 PyPI。交付宿主前，用包含迁移代码的候选宿主和脱敏的旧托管配置验收。临时环境仅使用 PyPI 安装的 Adapter，验证首次启动自动安装插件，以及重启后托管、账号 UID、顺序和设置仍然保留。
