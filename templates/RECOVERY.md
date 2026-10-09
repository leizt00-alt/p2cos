# p2cos v4 生成恢复模板

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
