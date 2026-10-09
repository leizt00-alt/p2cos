# p2cos

Pixiv 参考图到 Cos 图文的有状态工作流：保留原图与来源，逐页生成和独立质量检查，按持久任务恢复，并在发布前核对账号、图片、文案和 AI 声明。

本仓库是源码、测试及可复用配置/任务模板。运行需要外部 agent 编排、图像生成工具、已授权的 Pixiv 客户端与创作平台正常登录会话。Git 推送只更新源码仓库，**不会自动更新云端定时任务、部署文件或已有运行环境**。更新自动任务时应另外核对它实际加载的路径与版本。

## 安装与离线检查

当前脚本以 Linux、Python 3.12 及 `/workspace/p2cos` 为项目路径；部分浏览器和第三方适配路径使用该布局。迁移目录前需核对这些路径。

```bash
cd /workspace/p2cos
python -m venv /workspace/work/venvs/p2cos
/workspace/work/venvs/p2cos/bin/python -m pip install -r requirements.txt
/workspace/work/venvs/p2cos/bin/python -m unittest discover -v
```

只运行离线单元测试可安装 `requirements-test.txt`。测试在临时目录使用合成图片和模拟浏览器/客户端，不需要 Pixiv、Drive 或社交平台凭据。安装第三方上传适配器见 [模板说明](templates/SOCIAL_AUTO_UPLOAD.md)。

首次部署时，将 `workflow.example.json` 复制为 `workflow.json`，把 `templates/` 中的任务模板复制到项目根目录。已有环境应逐项合并更新，不要覆盖其运行配置、数据库和任务上下文。模板中的账号、Drive 文件夹必须自行配置（Drive字段是编排任务的目标配置，由正常Drive接口使用）；`observed_limits` 初始为空，需在当前平台正常界面确认图片数量、大小与格式限制后填写。占位配置不能直接发布。

Pixiv 客户端通过现有环境中的 `PIXIV_REFRESH_TOKEN` 进行正常 SDK 授权。不要把它写入 Git、配置示例或日志，也不要读取、打印、复制、导出浏览器 Cookie、Token 或存储值。已有浏览器资料目录只能由正常浏览器在同一环境内部使用。无会话时由用户正常登录。

## 执行与恢复

- 每次触发持久化一个 `run_id`。同次重试复用该 ID；新的明确请求创建新 run。同一 run 只选择一个作品，作品在跨 run 范围内去重。
- 月榜/日榜按上海日期交替，示例锚点为 `2026-10-09` 的月榜。定时准备和发布共用 `scheduled:YYYY-MM-DD`。提前发布仅限已保存明确授权的手动 run。
- 源特征分析、生成、质量复核、最终选择使用相互独立的 agent。所有原始页按源顺序保留，发布使用经过校验的完整图片组。
- 每页首次生成后最多两个正常优化槽；另有真实显式授权 policy 时最多一次未知调用 replacement，因此最多四次潜在工具调用，原未知记录仍保留。纠正调度前保存意图；保留实际参数、原图引用、工具回执和输出哈希。真实提示词和回执保存在私有运行数据中。
- 生成调用或发布点击结果未知时，先核实原执行结果。没有真正的幂等接口和未提交证据，不能盲目重发。恢复状态机及只读检查命令见 [恢复模板](templates/RECOVERY.md)。
- 上传适配器只点击一次发布按钮；超时、审核中或不明确的跳转进入核对流程。不得把标题匹配或页面跳转单独当成成功回执。

`p2cos_runs.py` 管理 run，`pixiv_pipeline.py` 管理选图与原图下载，`p2cos_state.py` 管理持久状态和门禁，`p2cos_recovery.py` 管理纠正恢复，`p2cos_watchdog.py` 只读报告已有 run 的阶段与下一步，`social_auto_upload_adapter.py` 提供受控发布入口。准备、发布和质量角色的操作步骤见 `templates/`。

## 公共导出范围

[`public-export.json`](public-export.json) 是本次公共文件的显式白名单。提交时仅按白名单添加文件，并检查暂存区；不要使用 `git add .` 或强制添加被忽略文件。`.gitignore` 默认忽略未知根目录文件，并额外保护浏览器资料、环境文件、数据库及侧文件、原图/生成图、压缩包、日志、工作输出和第三方源码/虚拟环境。

实际 `workflow.json`、根目录运行任务、生产数据库、运行历史、生成提示词、发布回执、截图、账号和 Drive 配置均留在运行环境。`run_quality_validation.py` 中的备份命令用于私有恢复，它创建的运行备份不属于公共源码导出。新部署如需导入自己已有的作品记录，应自行配置 `history_import`；示例不会默认导入任何个人作品列表。

有界自动恢复在公共配置中默认关闭，实际用户授权与 policy 存于私有 JSON。09:15/10:15 巡检使用同一 [恢复任务模板](templates/recovery_task.txt)，仅继续当天已有 scheduled job；云端部署须另行回读确认。年龄不代表失败，发布未知仍仅核查。
