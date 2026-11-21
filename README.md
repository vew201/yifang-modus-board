# 易方 MODUS 协作工作台

两人协作看板，包含登录、个人待办、项目八阶段、沟通意向、完成归档、成功/失败项目、项目外事项和文件管理。后端使用 Python 标准库与 SQLite；前端为 HTML、CSS、JavaScript。

## 本地开发

需要 Python 3.12，浏览器测试使用 Microsoft Edge。

```powershell
python scripts/fetch_assets.py
python server.py --port 8875 --data-dir .local/dev-data
```

打开 `http://127.0.0.1:8875`，设置测试账号。使用独立端口和测试库，避免写入正式数据。应用运行本身无需第三方 Python 依赖。

```powershell
python -m pip install -r requirements-dev.txt
python tests/run_all.py --browser --output .artifacts/tests
```

测试全部使用隔离数据库。无 Edge 的环境先只运行 `python -m unittest discover -s tests -v`。

中文字体为较大的第三方依赖，通过 `asset-manifest.json` 固定官方源码提交、文件大小和 SHA-256；上面的准备命令下载后验证，字体授权已包含在仓库。已有部署电脑上的同一字体会复用。

## 协作流程

1. `git switch -c feature/简短名称`，修改后提交并推送分支。
2. 向 `main` 发起 Pull Request，说明具体行为、验证结果和数据库影响。
3. CI 自动检查源码边界，并运行后端和浏览器测试。AI 按 `AGENTS.md` 审核当前提交；修改 PR 后需要重新核对新的提交。
4. AI 审核通过后，以真实的 GitHub `APPROVED` review 绑定当前 head SHA；在 PR 留下 `/modus merge` 触发合并检查。普通评论、点赞或“没有发现问题”的文字不代替审批。
5. 检查通过后合并 `main`。本机部署接入完成后，使用已审核的 `main` 更新服务，部署前备份，保留正式 `data` 和个人连接。

`XinboChen-Jason` 使用自己的 GitHub 身份提交 PR；仓库所有者的 AI 可以审核并合并这些变更。GitHub 不允许作者批准自己的 PR。可把 PR 链接发给负责维护的 Codex 对话，由 AI 检查差异、CI 与数据迁移后提交正式审核。

原生 Codex 自动审查需在 [Codex 代码审核设置](https://chatgpt.com/settings/code-review) 为本仓库开启。它与 GitHub 插件连接是两个设置，不能把插件已连接视为自动审查已启用。无需为本仓库额外提供 OpenAI API Key。

## 正式部署与版本

源码包含已通过测试的 `2026.11.21.1` 回收站升级。导入仓库时，本机正式服务仍为 `2026.11.20.3`，此前应用升级与重启命令被自动审批拒绝，尚需完成首次部署接入；代码存在于 `main` 不表示已经部署。

详见 [部署与协作设置](docs/部署与协作设置.md)。GitHub 托管源码，数据库、客户文件和账号凭证保留在部署电脑，不通过 Git 同步。部署环境的路径和凭据只写本机配置，不进入仓库。

## Codex 业务接入

登录工作台，在“Codex 接入与更新”生成本人私有首次接入包，交给专用 Codex 会话安装。包绑定生成它的账号，合伙人各自生成。后续按消息检查规则版本，有变化才下载更新，不设置客户端版本轮询任务。

`install_codex.py` 和“安装Codex接入.cmd”为旧版兼容入口，不能覆盖已经启用自动更新的客户端；新接入统一使用网页生成的本人首次接入包。

仓库不包含 `data/`、客户资料、个人接入包、数据库、会话、DuckDNS Token、看板连接 Token 或固定验证密钥。第三方字体授权随 `static/assets/*OFL.txt` 保存。
