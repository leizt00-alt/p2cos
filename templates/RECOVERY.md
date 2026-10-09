# p2cos v6 生成恢复模板

恢复使用原有 `run_id`、`job_id`、页索引和 `attempt_id`。不得删除纠正记录、清零次数、换生成者身份冒充原调用，或把未知结果视为失败后重发。

先使用只读检查：

```bash
cd /workspace/p2cos
python p2cos_recovery.py --db /workspace/p2cos/state.sqlite3 --job-id JOB_ID
```

可加 `--page-index PAGE_INDEX`。输出包含阶段、真实提交标识、本地请求/结果哈希，以及已保存结果的result_path、evidence_artifact、raw_result_artifact定位信息。这个命令不会查询外部工具、修正状态、生成图片或发布。需要迁移时先按运行维护流程保留私有数据库备份，由正常State初始化执行迁移；不要为了只读排查自行改表或记录。

| 阶段 | 含义与下一步 |
| --- | --- |
| `reserved` | 已占纠正预算；还未保存完整请求。确认原执行者已停止后，为同attempt保存真实完整参数。 |
| `prepared` | 请求已保存但未取得调度claim。只有本次原子claim成功的执行者能调用工具。 |
| `dispatching` | 已取得调度claim，工具可能已受理。重复claim不能授权重发；检查原执行会话或真实回执。 |
| `submitted` | 已记录工具真实返回的cell/request标识。仅用实际仍有效的原等待机制继续，不能凭标识自行重建调用。 |
| `result_saved` | 真实结果与证据已保存，尚未登记到页面。校验原输出后commit原结果，不调用工具。 |
| `recorded` | 该attempt已登记。继续独立质量复核，不重新生成。 |
| `unknown` | 旧pending记录缺少新调度证据。按未知处理，找原真实调用/回执，不能推断未提交。 |
| `failed` | 有绑定本attempt的真实终止工具失败证据。继续质量/预算流程；这不会自动增加可用次数。 |
| `confirmed_not_submitted` | 有权威证据确认未提交且原执行者已停止。复用同attempt重新prepare，预算不变。 |
| `abandoned_unknown` | 操作者显式放弃未知结果，保留审计和已耗预算。不是已完成纠正，不能用来满足最佳可用选择门禁。 |

正常纠正协议：

1. `State.begin_correction` 保存独立生成者、优化依据与检查目标，返回持久attempt。
2. `prepare_correction` 保存实际完整prompt、引用路径/角色/哈希及与之完全一致的 `tool_arguments`。当前页原图必须是实际引用；每个输出版本使用独立路径。
3. 紧邻真正工具调用前执行 `claim_correction_dispatch`；必须仅按该次成功返回的 `tool_arguments` 调用一次。`local_request_id` 只是本地审计标识，不能当作外部工具幂等键。
4. 只有工具真实返回cell/provider request ID时才 `note_correction_submission`。收到运行中cell时，只在原有效执行上下文继续等待。没有返回ID就不编造。
5. 工具返回成功后，先保存完整真实工具结果证据和图像，再 `save_correction_result`。证据须绑定job、页、attempt、原generator、真实prompt与输出哈希；证据文件及SHA-256要匹配。每个receipt还须绑定当前local_request_id和request_sha256；legacy没有这两者时，两个字段必须显式为null。
6. `commit_correction_result` 用保存的同一输出登记页面；重复同结果提交应仅确认既有结果。若中断发生在这里，恢复该提交，不再调用生成工具。

仅从未取得过dispatch claim的 `reserved`/`prepared` 纠正可以通过 `reassign_unsubmitted_correction` 交给新的独立生成agent：保存原/新生成者、恢复操作者与理由，保留同attempt和预算，清除旧prepared请求并回到reserved，由新执行者重新prepare。必须停止原执行者；不能对dispatching/submitted/unknown，或有过claim但后来确认未提交的attempt使用此交接入口。

恢复操作者可以是另一agent，但已执行工具调用的证据中的 `generator_agent_id` 必须保留原生成者；操作者使用自己的 `operator_agent_id` 并描述真实观察。静态JSON证据只能做格式/绑定验证，不能凭空证明远端事实，禁止手写成功回执或给未知调用补造“失败”。合成测试fixture不可用作生产证据。

证据JSON大小不得超过1 MiB，不能内嵌base64/data URL。若真实工具返回包含图片二进制，完整原始响应单独保存在私有 `raw_result_artifact`，以 `{path, sha256}` 引用；小证据中的 `tool_result` 只保留真实非二进制字段。真实返回只有二进制时，允许 `tool_result: {}` 与已校验的完整原始响应artifact一起保存；不得为填字段编造metadata。

`fail_correction` 仅接受真实终止工具失败和匹配prompt的证据。超时、代理中断、没有本地文件、agent停止、日志缺失都无法单独证明失败或未提交。确认未提交必须有权威未受理证据，并确认原执行者停止；才可通过 `resolve_correction_unknown(..., resolution="not_submitted", evidence=...)` 复用同attempt。

已提交但无法继续查询时保留阻碍。没有通用的工具结果查询/幂等接口，不能声称这些状态记录保证远端恰好执行一次。发布的未知提交另按SOCIAL_AUTO_UPLOAD.md核对，不能把生成恢复当作重新发帖授权。

## v5 有界恢复与只读巡检

`p2cos_watchdog.py --config /workspace/p2cos/workflow.json` 只读检查真实 Asia/Shanghai 当天已有 scheduled run；可加 `--run-id RUN_ID` 精确检查另一个已有 run。它只用 SQLite `mode=ro`/`query_only`，不构造 State、不迁移、不创建 run/job、不选新图、不生成、不发布。输出 run/job、stage、age、deadline、next_action、needs_attention、正常/额外预算及未知记录；age/缺心跳不是失败证据，completed_quality_slots 留 null 等待真正 State 质量核验，不能据健康输出宣称 ready。

每页保留初版、最多 2 个正常优化槽，以及独立的最多 1 次未知调用 replacement；因此最多 4 次潜在工具调用（含原未知调用），不能写成总 3 次。replacement 与原优化槽相连，不清零 correction_count、不覆盖旧记录、不伪造失败。原记录保持 superseded_unknown/outcome=unknown；replacement 不得再次 replacement，也不得通过 reassign 改变其授权绑定的新执行者。若原结果后来出现，用 audit_late_superseded_result 保留审计，不覆盖当前候选、替换来源或再发一次。未知、放弃未知、superseded_unknown 自身都不是完成质量槽；只有该槽真实 recorded 且具有效独立QA的候选或真实终结失败证据，才由 State 判断其完成资格。

自动恢复默认关闭，只有私有 workflow.recovery.auto_unknown_recovery_enabled=true、匹配文件 hash 的 enabled policy 及真实 explicit_user_authorization 同时有效才可启用；本部署的有效授权按私有文件核验，不再逐次询问已授权的同范围恢复。公共模板中的占位授权没有效力。每次先找真实原结果/可续接原调用：result_saved 仅 verify+commit 后交全新独立QA，recorded 继续独立QA。普通 reserved/prepared 只有历史从未 dispatch_claimed 且原执行者不可用时才审计 reassign 给全新生成者、重新 prepare、首次 claim；replacement 的 reserved/prepared 只能恢复授权绑定执行者，不能转让身份。提交过的调用不能借此重派。

unknown/dispatching/submitted 的自动 replacement 还须有真实停止证据：有真实权威观察证明原本地 executor 已停止，并确认不能续接原调用，记录真实工具观察；仅失联、查无活动记录、无法续接本身均不证明停止；仅时长、目录空、无输出、到达发布时间或短阈值绝不触发并发替换。停止本地执行者仍不证明远端停止或失败。executor_stopped JSON 须绑定 run_id、artwork_id、job_id、page_index、attempt_id、generator_agent_id、local_request_id、request_sha256（legacy 两项显式 null），outcome=stopped、original_call_recoverable=false 和具体 statement；真实回执以路径/hash保留，不伪造事实。可将引用列表保存为私有 JSON 的 executor_stop_evidence 数组，以 `--evidence-manifest MANIFEST_PATH` 供 watchdog 复核。缺证据、policy无效或额外预算已占用只能告警并保留未知。

符合条件后由新执行者调用 `State.supersede_unknown_with_authorization(job_id, page_index, attempt_id=旧ID, generator_agent_id=原ID, new_generator_agent_id=真实新ID, operator_agent_id=真实调度ID, optimization=完整继承原attempt已保存optimization并核验其仍对应当前最新已记录独立QA的对象, authorization={path,sha256}, mode="explicit_policy", policy={path,sha256})`。authorization 文件 kind=authorized_unknown_replacement，逐项绑定 run/job/artwork/page/attempt、原/新generator、operator、mode、local_request_id、request_sha256；含 accept_unknown_outcome=true、original_executor_stopped=true、statement、executor_stop_evidence 引用、user_authorization 引用。policy kind=bounded_unknown_recovery_policy、enabled=true、scope=all_runs 或精确 run_id、max_unknown_replacements_per_page=1、generation_only=true、unknown_publication_action=reconcile，且引用同一真实用户授权；source kind=explicit_user_authorization、scope=bounded_unknown_recovery_policy，保存真实 source/user_message。所有引用只含实际文件 path/sha256。不能把布尔配置、fixture、占位文本或自己写的声明当真实授权/停止事实。State 原子校验同页额外预算、原incarnation、全新身份和当前优化依据；随后仍须 prepare→首次 claim→真实调用→同执行链立即保存真实结果/trace→save→commit→全新独立QA。正常两次优化、耗尽择优与全部发布硬门禁不变。

每日 08:00/10:00 原任务都在恢复检查中应用同一 policy。另一个 recovery_task.txt 模板可部署为单个自动任务、每天上海 09:15 与 10:15 各运行一次：仅恢复当天已有 scheduled run 的原 job，无 job 则报告，不创建 run/选图；09:15 只准备；真实10:00后才可按原发布流程对 ready 作品补发。已发布或 verified 的任务仅跳过再次生成/发布，仍检查并由全新 fork_turns="none" 存储 agent 补齐全部候选、正式 QA/选择、已验真发布回执及索引；归档同步完整才结束，绝不再次提交。发布 unknown/in_flight/reconciliation_required/审核中永远只 remote reconcile，列表暂未看到帖子不是未提交证据，不能借生成恢复重复投稿。原 social-auto-upload 优先、明确未提交证据下的官方 UI 回退及所有共享门禁保留。

本地模板变更不等于云端部署；部署者须更新原08/10 prompt并回读 v6、时区与时刻，再创建/更新唯一双时刻巡检任务并回读，避免重复调度。此文件本身不创建云任务。白名单私有 Drive 备份增加 p2cos_watchdog.py、recovery_task.txt、recovery_policy.json、recovery_authorization.json 及本次真实停止/授权回执；policy/用户原文/实际运行事实不得进入公共 Git，既有凭据/Cookie/profile排除规则不变。

v6 全版本归档衔接
每个真实输出完整解码/hash验证并 record_cos 或 save_result→commit 为 recorded 后，立即派全新 spawn_agent(fork_turns="none") 存储 agent 按 RUNBOOK.txt 的“v6 全部真实成品归档与发布标注”规则上传 Drive candidates/p{page}/v{physical_attempt}/；不等 QA 或 State.publish_ready，QA 尚无则 score=null、qa_status=pending，正式结果随后幂等更新。恢复 recorded 同样检查补齐归档并派另一个全新独立 QA，unknown/failed 无真实图只留真实证据，不冒充生成。全部候选包括低分和最终选中版本永久保留；根《生成记录与发布状态.txt》与 manifest.json 分开记录尝试序号 physical_attempt+1、按真实 recorded 顺序稳定分配的确认成功生成序号及 generated/selected/published。只有原最终门禁及 State.publish_ready 通过才复制选定整套到根 cos，字节/hash必须匹配 selected candidate；只有独立发布验真回执才能 published=true，必须绑定选定版本/hash与真实URL/ID或严格UI回执null ID。更新选择清旧selected但不改published历史，状态变化幂等同步并回读核验。完整目录、证据、命名与回读细则以 RUNBOOK.txt v6 为准；原 v5 有界恢复授权与预算、安全发布职责保持不变。
