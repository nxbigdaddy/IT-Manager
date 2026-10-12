# 后端专项审计：事务、授权、历史与使用手册边界

## 范围、方法与结论

> 证据基线：下方B01–B06保留审计时的修复前现象和行号，不应直接解释为当前仍存在的缺陷。父任务随后明确修订阶段性约束，授权后端修复；当前状态见以下“修复追记”。

### 修复追记（优先于下方历史基线）

- **B02已修复并正式回归**：[Store原子版本接口](../itmanager/store.py#L560) 将ID检查、版本比较、提示词历史、默认撤销（递增版本并留历史）、新值和审计放在同一数据库事务。[提示词路由](../itmanager/server.py#L171-L174) 已接入。新增失败请求无副作用、事务故障回滚、跨Store并发唯一默认回归。
- **B03已修复并正式回归**：[专项entry保存](../itmanager/workspaces.py#L311) 在数据库事务内再次校验版本，两个独立Store相同版本竞争仅一个成功。
- **B04支持显式CAS**：[模板服务](../itmanager/workspaces.py#L210-L212) 及 [模板路由](../itmanager/workspace_routes.py#L11) 接受 version；携旧version拒绝。为保留旧API，省略version仍为历史兼容的后写覆盖；GUI需携当前version，由父任务完成。
- **B05 period_days已修复**：[server.py:180–181](../itmanager/server.py#L180-L181) 严格整数1–366，null/bool/string/float/越界均400。AI record_ids类型/hash由父任务修改，其专项验证由父任务汇总。
- 正式回归新增 [test_audit_backend_fixes.py](../tests/test_audit_backend_fixes.py)：跨Store表单CAS、模板旧版本拒绝及无version兼容、提示词原子性/历史/故障回滚、并发默认唯一、period_days六种非法值，共10项。
- 修复后执行 `python -m pytest -q tests/test_audit_backend_fixes.py tests/test_store.py tests/test_server.py tests/test_workspaces.py tests/test_management_ops.py tests/test_management_routes.py tests/test_ai_form_server.py tests/test_validation.py`：**162 passed，1 warning，35.35s**。后台job已收集完毕。下方旧复现脚本断言的是修复前缺陷，不应再作为当前验收测试。
- B06 GUI历史实际能力、其他静态风险仍应按产品现状限定，未在本修复中新增历史浏览/恢复界面。

审查 SQLite 后端 Store、server、workspaces、workspace_routes、ai_forms、validation，并交叉核对 GUI 与现有测试；**审计阶段未修改产品源代码，后续按父任务明确修订的委派实施上述有限修复**。新增独立复现脚本 [audit_backend_repro.py](../tests/audit_backend_repro.py)，文件名不符合 pytest 默认 `test_*.py` / `*_test.py` 收集模式，仅显式执行。全部复现使用临时数据库；AI 调用 mock，无真实模型请求、费用或生产数据变更。

优先处理：**普通 AI 填表预览与实际发送内容不绑定、提示词失败请求仍产生部分写入、专项表单跨实例并发丢失版本**。这是单可信管理员系统，以下不是多租户越权结论，也未声称所有业务写入都缺乏事务。

### 实际执行

在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe tests/audit_backend_repro.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_store.py tests/test_server.py tests/test_workspaces.py tests/test_management_ops.py tests/test_management_routes.py tests/test_ai_forms.py tests/test_ai_form_server.py tests/test_validation.py
```

- 独立复现：**6 tests，全部通过，5.053s**。这些断言证明当前缺陷存在，不代表缺陷修复后应继续通过。
- 现有回归：**166 passed，1 warning，29.36s**。警告为 FastAPI TestClient 使用 httpx 的弃用提示，无测试失败。
- 本次未执行 GUI 自动化/实际点击验收、真实外部 API、真实多进程服务启动或故障断电测试。跨实例竞态使用两个独立 Store、线程 barrier 精确安排合法读写交错，不是以随机压力替代证据。

## 前三重点（已运行复现）

### B01 / 高：普通 AI 填表授权预览不是实际外发内容的快照

**证据**：[ai_form_ui.py:213–254](../itmanager/ai_form_ui.py#L213-L254) 用本地缓存记录展示预览，但请求只有 `context_ids`；[server.py:218–225](../itmanager/server.py#L218-L225) 按 ID 重取当前数据库内容后直接调用模型；[ai_forms.py:175–185](../itmanager/ai_forms.py#L175-L185) 把重取的内容写入模型提示词。接口未接收选中记录版本或授权 hash。

**复现输入/步骤**：
1. 建任务 `{'名称':'Preview public','管理建议':'Public text'}`，客户端缓存 v1 并据此预览。
2. 第二客户端以 version=1 保存同一任务，管理建议改成 `PRIVATE_AFTER_PREVIEW`。
3. POST `/ai/form-fill`：`module='tasks'`、有效 provider/prompt、`context_ids=[任务ID]`、`current={}`、`basis=''`、`confirmed=true`、`sensitive_confirmed=false`。
4. 模型 mock 捕获实际 prompt，含 `PRIVATE_AFTER_PREVIEW`，HTTP 200。脚本用例 `test_form_ai_sends_changed_context_without_preview_binding` 已验证。

**影响**：未在刚才预览中出现的新内容可能外发。敏感二次授权按模块判定，任务自由文本中的敏感信息不自动识别；本问题不要求恶意用户，另一窗口正常保存即可触发。

**建议**：参考专项 AI 的服务器预览/hash机制，绑定记录完整内容/版本、提示词版本和模型配置身份；提交时不一致拒绝并重新授权。手册目前只能说“预览本地当前表单与缓存的选中材料；服务器会重读关联记录”，不能保证普通填表的逐字快照一致。

**对照**：专项路由 [workspace_routes.py:32–55](../itmanager/workspace_routes.py#L32-L55) 已验证 `context_hash`，且无条件二次授权；不能把 B01 泛化成所有 AI 路由都没有保护。普通 `/ai/generate` 同样按 ID 重读且无 hash：[server.py:241–254](../itmanager/server.py#L241-L254)，本次未独立运行该路径的预览竞态，归为同机制静态风险。

### B02 / 高：提示词请求失败仍改变默认与历史；默认切换没有完整版本轨迹

**证据**：[server.py:173–191](../itmanager/server.py#L173-L191) 在最终保存前分别提交 history 和其他提示词默认状态；[store.py:545–558](../itmanager/store.py#L545-L558) 每次 put 独立事务，且直到最后的 put 才拒绝 payload ID 不匹配。

**复现 A（400 仍改库）**：
1. 获取 tasks 默认填表提示词（原 version=1、is_default=true）。
2. 复制其 data，令内层 `id='wrong-payload-id'`、名称任意，保留 `purpose='form_fill'`、`module='tasks'`、`is_default=true`。
3. POST `/objects/prompts`，只传上述 data、不传外层 id。
4. 返回 HTTP 400“对象ID与目标ID不一致”，但原默认已变 false；提示词总数不变，该模块无默认。
5. 再用外层 id=原提示词 ID 提交内层错误 id；仍 400，却增加一条 prompt_history，当前版本仍 1。

**复现 B（正常默认切换丢轨迹）**：复制同一默认提示词，删除 data.id，用新名称新增并设默认。200 后原提示词 `is_default=false`，但 version 不变、无对应 prompt_history。

独立用例 `test_failed_prompt_write_still_mutates_defaults_and_history`、`test_prompt_default_has_no_version_or_history` 均通过。

**影响**：失败不等于没有持久变更；历史会记录未完成编辑，默认状态变化无法按提示词版本还原。GUI 正常提交一般不会构造错误 ID，但 API 是受支持输入面；中间故障也有同类非原子风险。

**建议**：先完整校验所有 ID/类型，再把 history、新值、其他默认撤销放在一个 `BEGIN IMMEDIATE` 事务；默认变化也应生成版本历史或专门默认切换审计事件。添加数据库级/事务级“每模块至多一个默认”的维护。并发切换出现多默认是静态可能性，本次未运行并发默认切换，不将其写成已验证。

**现有测试缺口**：[test_ai_form_server.py:49–62](../tests/test_ai_form_server.py#L49-L62) 只检查单次正常切换后默认唯一、编辑目标版本增加、历史非空；未检查被撤销默认者历史或失败原子性。

### B03 / 高（多实例部署）；单实例低：专项表单的版本校验不是跨实例 CAS

**证据**：[workspaces.py:284–320](../itmanager/workspaces.py#L284-L320) 在 Python Store 对象的 RLock 内读取、比对版本、拼接 oldhistory，随后独立 put；[store.py:36](../itmanager/store.py#L36)、[store.py:555–557](../itmanager/store.py#L555-L557) 每个实例自己的锁，最终 SQL 是无条件 upsert。源码模块注释已明确“不提供 cross-process CAS”：[workspaces.py:1–6](../itmanager/workspaces.py#L1-L6)。

**步骤**：建专项和一条 evidence 表单 v1（subject=initial）；两个独立 Store 连同一 SQLite 文件，分别用 version=1 写 subject=writer-0 / writer-1。在两个 `get_entry` 都完成、put 尚未执行的自然可发生位置用 barrier 安排交错。两请求均成功返回 v2；最终只有一个 writer 值，oldhistory 只有 initial v1，另一份已接受值彻底不在版本历史。

脚本 `test_entry_cross_store_loses_one_accepted_revision` 已运行通过。未实测多进程 HTTP，但双 Store 的数据库读写路径与多 worker 中无共享锁的前提一致。

**影响**：两个服务/worker共享库时静默覆盖，版本号重用。单个 create_app/Store 内的同进程线程操作受锁保护，不能误报为所有并发编辑都丢失。

**建议**：把读取、version 比较、历史和写入纳入同一 DB 写事务，或 SQL 条件更新并检查 rowcount；在此之前手册明确“单服务单 Store；不要多个 worker/服务共享写库”。[专项设计:14](13_专项工作空间与配置式扩展.md#L14) 的“版本CAS”应加限制，虽 [专项设计:25](13_专项工作空间与配置式扩展.md#L25) 已写 SQLite 单服务。

**现有测试缺口**：[test_workspaces.py:105–122](../tests/test_workspaces.py#L105-L122) 只验证单 service 的顺序旧版本拒绝。不要与基础记录混同：基础记录真正先 `BEGIN IMMEDIATE` 再读版本，现有 [test_store.py:150–161](../tests/test_store.py#L150-L161) 已覆盖跨 Store 竞争且本次通过。

## 其他已验证问题 / 功能边界

### B04 / 中：专项模板旧编辑器可顺序覆盖他人修改，无 expected_version

**证据**：[workspace_routes.py:10–11](../itmanager/workspace_routes.py#L10-L11)、[workspaces.py:200–214](../itmanager/workspaces.py#L200-L214)、[workspaces_ui.py:352–361](../itmanager/workspaces_ui.py#L352-L361)。

**步骤**：A/B 同时取得模板 v1；A 改 description 保存成为 v2；B 用旧完整配置只改 name 保存，仍 200 得 v3，description 回退 v1。`test_template_stale_editor_silently_overwrites` 已验证；不需要并发执行。历史仍保留 A 的 v2，不等于历史丢失，但没有 GUI 回滚保证。

**建议/手册**：保存提供 expected_version，过期提示重载；当前模板“保存为新版本”只代表版本递增/留快照，不代表防覆盖。普通 prompts 也未提供期望版本检查，属同类静态边界。

### B05 / 低至中：部分结构错误穿透为 HTTP500，不是统一可解释的400

**证据**：[server.py:241–248](../itmanager/server.py#L241-L248) 未验证 record_ids 元素类型便 set(ids)；[validation.py:46–48](../itmanager/validation.py#L46-L48) 不验证 period_days；[server.py:188](../itmanager/server.py#L188) 调用 int；异常映射仅覆盖 ValueError：[server.py:56–61](../itmanager/server.py#L56-L61)。

**步骤**：有效 provider/prompt 下 POST `/ai/generate`，`record_ids=[{}], confirmed=true`，得到 500（unhashable dict）；POST `/objects/schedules`，data=`{'kind':'report','mode':'draft','schedule':{},'period_days':null}`，得到 500（int(None)）。`test_malformed_inputs_return_500` 已验证，未产生模型网络请求。

**影响/建议**：错误被误当成服务故障，客户端提示/重试失真；这是认证管理员输入鲁棒性问题，不是未认证远程执行。采用明确请求 schema/元素及数值类型校验，返回400/422；不要仅全局吞 TypeError 掩盖编程错误。

### B06 / 已静态确认的 GUI 差异：有“专项表单历史”入口，但没有逐版本历史查看入口

**准确描述**：GUI 路径为“专项工作空间 → 进入工作空间 → 专项表单历史”tab，并有“编辑历史（版本校验）”按钮。[workspaces_ui.py:252–294](../itmanager/workspaces_ui.py#L252-L294) 表格只填每条 entry 的 `form_key/status/version/data/id`，编辑载入当前 data/version：[workspaces_ui.py:296–310](../itmanager/workspaces_ui.py#L296-L310)。它不是 oldhistory 时间线。

**API/后端**：GET `/workspaces/{id}/entries` 返回每条记录的完整 oldhistory，含旧快照；[workspaces.py:272–275](../itmanager/workspaces.py#L272-L275)、[workspaces.py:313–320](../itmanager/workspaces.py#L313-L320)。未提供专门历史/还原路由。模板列表也带 oldhistory，模板导出明确只导出当前 config：[workspaces.py:216–218](../itmanager/workspaces.py#L216-L218)。

**人工验证步骤（本次未点击执行）**：新建专项 evidence v1→改证据内容保存 v2→打开“专项表单历史”，表格只见 v2 当前内容；通过 API 检查 oldhistory 可得 v1。源码路径完整静态核对，但不能当作实际截图/交互验收通过。

**影响/建议**：手册不应写“可在专项表单历史页浏览各历史版本/恢复旧版”；应写“此页列出已填写表单的当前版本，可继续编辑，后端保留历史快照但暂无逐版本查看/恢复GUI”。可新增只读 oldhistory 明细和显式复制旧值为新版本功能。普通提示词另有真实“历史版本”GUI：[gui.py:368–381](../itmanager/gui.py#L368-L381)，不要说整个产品都没有历史入口。

## 仅静态风险与手册需限定事项（未作为已执行漏洞）

1. **AI 审计字段并不在所有入口一致**。专项 AI 的 call 仅 purpose/workspace_id/model/prompt_name/status/usage，缺 time/provider/prompt_version/context_hash；草稿也没有 call_id：[workspace_routes.py:54–59](../itmanager/workspace_routes.py#L54-L59)。普通生成和填表有更多版本/时间/调用 ID：[server.py:224–230](../itmanager/server.py#L224-L230)、[server.py:256–259](../itmanager/server.py#L256-L259)。因此 [安全设计:16](05_集成AI与安全设计.md#L16)“调用模型/提示词版本/用量/状态可查询”不应无条件覆盖专项调用。建议统一元数据并保存授权摘要，不必保留敏感原始正文。
2. **专项预览 hash 不绑定模型配置**。hash 只覆盖 render_context，provider 在发送时重新读取：[workspace_routes.py:35–50](../itmanager/workspace_routes.py#L35-L50)。相同 provider_id 的 model/base_url/配置在预览后改动，不会令资料 hash 失效；尚未实际运行竞态。建议将所授权的模型配置指纹纳入确认令牌。模板修改不改变既有专项快照，这是设计，不是缺陷。
3. **专项 report 不是单一读事务快照**。阶段、关联记录、表单通过多次连接读取：[workspaces.py:360–383](../itmanager/workspaces.py#L360-L383)。并发更新中可能得到逻辑上混合时点的报告；专项发送 hash 会防止与已预览文本不同，但不等于报告内部所有数据同一数据库快照。建议只读事务集中采样；本次未运行混合快照复现。
4. **数据范围是结构关联，不是权限隔离，也不递归加载所有外键**。[workspaces.py:330–345](../itmanager/workspaces.py#L330-L345) 包含专项 project、项目内当前 tasks、关联到这些锚点的 risks/logs、显式 links；不会按 owner/name 扩散，也不会仅因人员ID/系统ID就自动纳入 people/systems。显式关联任何未归档基础记录是管理员授权功能。只有 POST link、没有解除关联路由：[workspace_routes.py:19–31](../itmanager/workspace_routes.py#L19-L31)。错误关联后缺少专用解除GUI/API，超长 AI 提示“减少关联记录”并无对称 unlink 操作。建议补安全解绑；不能建议直接改数据库作为普通用户流程。
5. **敏感模块识别不等于内容级 DLP**。普通生成/填表按 people/reviews/access 判定二次授权，basis/current/其他模块自由文本不扫描敏感性：[server.py:222–223](../itmanager/server.py#L222-L223)、[server.py:245–247](../itmanager/server.py#L245-L247)。专项则统一二次确认。手册必须要求用户人工检查所有自由文本，不保证“含敏感信息必弹二次确认”。
6. **AI 填表做结构/保护字段过滤，不保证事实正确**。[ai_forms.py:188–240](../itmanager/ai_forms.py#L188-L240) 拒绝未知字段、忽略保护字段、校验引用/人员/日期/数值、压初始状态，但普通描述文本仍可包含幻觉；suggestions 不经语义事实验证。另基础存储允许文本最长50000，填表 context 每字段5000：[store.py:101–102](../itmanager/store.py#L101-L102)、[ai_forms.py:17–20](../itmanager/ai_forms.py#L17-L20)、[ai_forms.py:173](../itmanager/ai_forms.py#L173)。合法长记录可能无法用于AI填表，这不是数据库保存失败。
7. **“幂等”不是所有 POST 的属性**。管理动作/行动项/检查表/专项创建有 receipt 和参数指纹；普通新增业务记录、专项表单新增、报告、AI 调用无请求去重，重试可能重复记录或费用。手动发送按 request_id 去重但不比较本次其他参数：[store.py:595–598](../itmanager/store.py#L595-L598)；错误复用同一key会返回duplicate，而非重新发送或参数冲突。手册应要求每次独立操作新key、不确定重试保留原key；不是跨第三方 exactly-once。
8. **归档与导出不是全库迁移**。归档检查基础记录 REFERENCES，不检查 workspace_links/task_ids；归档后专项范围只读未归档记录，阶段初始任务仍计 missing：[store.py:452–467](../itmanager/store.py#L452-L467)、[workspaces.py:348–357](../itmanager/workspaces.py#L348-L357)。GET `/export` 只导出未归档基础 records，不含专项、配置、历史、审计或凭据：[server.py:311–312](../itmanager/server.py#L311-L312)。全库备份用 SQLite backup；不能把 JSON 导出称为全功能备份。

## 已有保护与测试覆盖：避免重复泛泛审计

| 已检查能力 | 当前实现/实际测试结果 |
|---|---|
| 基础记录保存/归档/导入事务与历史 | [store.py:172–200](../itmanager/store.py#L172-L200)、[store.py:524–535](../itmanager/store.py#L524-L535) 在事务内写记录、快照和审计；跨Store CAS现有测试已通过，不报“所有保存无CAS”。 |
| 管理闭环原子/幂等 | [store.py:263–316](../itmanager/store.py#L263-L316)、[store.py:409–444](../itmanager/store.py#L409-L444) BEGIN IMMEDIATE、参数指纹、回执同事务；现有 [管理测试:237–262](../tests/test_management_ops.py#L237-L262) 包含注入异常回滚、跨实例重放，本次通过。 |
| 草稿批准与发送绑定 | [store.py:560–624](../itmanager/store.py#L560-L624) revision校验、approved_revision与正文快照；后续撤销不取消已经claim的发送，是明确线性化边界，不是泛化TOCTOU。 |
| 专项范围与模板快照 | 现有 [专项测试:67–80](../tests/test_workspaces.py#L67-L80)、[专项测试:124–160](../tests/test_workspaces.py#L124-L160) 验证模板更改不改旧空间、跨专项排除、显式关联与后续行动；本次通过。 |
| AI授权与清洗 | 现有 [AI路由测试:41–46](../tests/test_ai_form_server.py#L41-L46)、[专项路由测试:27–40](../tests/test_management_routes.py#L27-L40) 覆盖严格布尔/敏感确认与stale hash，但不覆盖普通预览后记录变化。AI forms回归本次全部通过。 |
| 配置校验 | 白名单、布尔不接受truthy字符串、官方端点/密钥环境引用有现有测试；本次不是重复报告缺认证/任意URL，新增B05只针对未覆盖结构类型。 |

## 使用手册建议采用的精确表述

- 单可信管理员 Bearer 服务；专项只是数据组织范围，不是部门/用户权限屏障。SQLite 建议单服务单写实例，不承诺多个 worker 的专项表单CAS。
- 专项创建一次事务完成项目/工作包/空间快照；模板后续更改仅影响新空间。模板编辑会增版本，但不会拦截旧编辑器覆盖。
- 阶段百分比仅按初始工作包统计；后续行动虽进入专项，不自动分配阶段，不代表所有工作已完成或合规通过。
- “专项表单历史”展示当前表单记录及版本；旧快照后端保留，暂无逐版本浏览/恢复GUI。draft也必须填完模板 required 字段，submitted不是审批锁定状态：[workspaces.py:293–320](../itmanager/workspaces.py#L293-L320)。
- 提示词有编辑版本历史与普通历史GUI；默认切换对被取消默认的模板不记版本历史。不要称为所有属性变更完整可追溯。
- 专项 AI 完整范围服务器预览+hash核验+二次授权；普通填表预览当前本地材料但发送时按ID重读，暂不保证预览快照。自由文本是否敏感须人工判断。
- AI 填表生成不保存业务记录；应用字段后仍需人工保存。用户另行点击“选择管理建议，建立正式任务”可显式创建行动，不应写成完全没有创建入口：[ai_form_ui.py:157–160](../itmanager/ai_form_ui.py#L157-L160)、[ai_form_ui.py:262–266](../itmanager/ai_form_ui.py#L262-L266)。专项动态表单没有结构化AI自动填字段。
- JSON导出仅基础当前记录；完整恢复应使用离线SQLite备份/恢复流程，不能以模板导出或records导出替代。
