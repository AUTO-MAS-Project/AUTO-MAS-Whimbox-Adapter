# AUTO-MAS Whimbox Adapter

AUTO-MAS 的奇想盒（无限暖暖）专项插件。

插件调用奇想盒官方 `startOneDragon` 入口，消费其日志结果；保留原生配置、MAS 账号覆盖、配置备份恢复、后端更新和通知能力。

## 本地开发

将仓库克隆到 AUTO-MAS 主仓的 `plugins/whimbox_adapter`。在主仓执行 `yarn dev`，然后在插件中心启用源码插件。前端目前随主程序构建，入口为主仓 `frontend/src/views/whimbox/entry.ts`；主程序尚不支持独立加载插件前端。

旧托管仍使用 `WhimboxConfig` / `WhimboxUserConfig` 类型及原有 UID。插件在激活时将 `UserData` 转为 `users`，不重建账号。禁用沿用宿主语义，会删除本插件的托管和账号；操作前应先备份。

## 验证与构建

使用与本插件匹配的 AUTO-MAS 源码及开发依赖。设置 `AUTO_MAS_HOST_SOURCE` 为主仓绝对路径，然后执行：

```powershell
python -m pytest tests -q
python -m ruff check src tests
python -m ruff format --check src tests
uv build
```

正式安装应使用发行 wheel；本地源码发现与发行包安装是两个入口。首次发行前，旧托管的自动安装迁移需要使用本地源码插件验证。

首发需与包含 Whimbox 插件迁移的宿主版本配套。发布前根据实际 SDK 发行版本收紧依赖下界，
并验证该发行包；当前开发版版本号不能证明旧版 SDK 已包含迁移需要的接口。

本次迁移未改变上游游戏执行与结果判定语义。真实游戏、管理员授权和线上更新需在装有奇想盒的 Windows 环境验收。
