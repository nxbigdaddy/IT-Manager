# 桌面 GUI 实际操作审核

## 范围与方法

覆盖 [gui.py](../itmanager/gui.py)、[forms.py](../itmanager/forms.py)、[workspace_ui.py](../itmanager/workspace_ui.py)、[window_chrome.py](../itmanager/window_chrome.py)、[workspaces_ui.py](../itmanager/workspaces_ui.py)、[checklists_ui.py](../itmanager/checklists_ui.py)、[management_ui.py](../itmanager/management_ui.py)、[ai_form_ui.py](../itmanager/ai_form_ui.py)。

在 Windows / Python / PySide6 6.8.2.1 的 offscreen QApplication 中创建实际 MainWindow/FormDialog，点击真实 Qt 按钮；以可控延迟 runner 替代网络，精确控制导航与请求返回先后。没有调用外部模型、发送通知、读取生产令牌或修改业务库。offscreen **不能证明原生 Windows 拖动/边缘缩放、多屏 DPI 或视觉截断**；这里只证明 Qt 关窗与最大化状态逻辑。

最初先只读源码，新增 [audit_gui_repro.py](../tests/audit_gui_repro.py) 复现；父代理随后明确修订委派，允许修复两个专项/检查 UI 文件并增加正式回归。下面保留修复前观察与现有结果，不把旧缺陷写成仍未修复。

## 确认并已修复的严重问题

### G1：专项保存期间可继续编辑，成功返回静默丢掉新增输入（高）

**真实路径：** 项目规划 → 专项工作空间 → 进入 → 填写专项表单；填入内容后保存，服务响应前继续补充证据。

**修复前证据：** `_ws_bind_save` 只把 Save 按钮禁用，`dialog.busy=False`，字段仍 enabled。复现首次提交 `name=提交前`，随后改为 `提交后补充的重要证据`；实际 POST 仍是旧值，成功回调直接 `accept()`，编辑器关闭且没有脏输入确认。相同机制影响新建专项、专项历史编辑、关联基础资料。

**修复：** [workspaces_ui.py](../itmanager/workspaces_ui.py) 提交前 `set_busy(True)` 冻结整个表单及取消；失败解锁并保留输入；成功解锁后关闭。回归同时验证保存中 reject 无法关闭。

### G2：主刷新延迟返回使已打开专项表单永久失效（高）

**真实路径：** 在专项页点击主工具栏刷新；网络慢时立即打开新建/编辑专项表单；刷新完成后保存。另一顺序是表单先保存，再收到先前发起的主刷新结果。

**修复前证据：** 主刷新完成会调用 `navigate()`，递增 generation。专项表单持有的 `_ws_guard()` 永远为 false：

- 未提交：Save 点击不发任何请求，无错误提示，编辑器仍留在屏幕。
- 已提交：后台成功保存，但 `done()` 因 guard 失效连重新启用按钮都不做，Save 永久灰，用户无法判定是否保存。

复现直接执行主刷新完成时同一条 `navigate(workspaces)` 路径，并测得 pending 请求数为 0 / 保存按钮 disabled；不是依赖偶然线程时序的推测。

**修复：** 模态表单请求绑定 client 与 dialog 生命周期，不绑定可重建的底页 generation。工作库变化显式提示；旧页面回调不再触碰旧控件，只刷新当前专项页；匹配的创建 request_id 成功后清除暂存。回归覆盖刷新发生于提交前/提交后，以及刷新后的失败解锁与输入保留。

### G3：检查提交中切走又切回，成功/失败后新页面仍锁死（中）

**真实路径：** 日常检查全部填完提交 → 网络尚未完成时切到工作台 → 马上切回日常检查 → 等结果。

**修复前证据：** 新页面按同一个 `attempt.busy=True` 禁用编辑器和“新建检查”；旧成功回调虽将 attempt 改为 completed，却因旧 generation 失效提前返回，不刷新新页面。“新建检查”仍 disabled，完成提示仍为空；必须再次切页才能恢复。失败路径同样无法恢复重试操作。

**修复：** 当前 client 与 attempt 身份匹配时将完成/失败信息写回当前检查页面；离页错误也保存到内存提示。已替换的 attempt 和其他工作库仍不更新。回归覆盖返回后的成功解锁及失败重试解锁。

### G4：专项模板编辑携带版本供后端 CAS 校验

应父代理与后端并发审核协调，编辑已有模板现在同时传 `id` 和读取时 `version`；新建/复制不传这两个字段。[正式回归](../tests/test_gui_audit_regressions.py) 断言 v7 编辑请求携带 version=7、复制请求无版本。此项后端冲突返回与持久化原子性由后端审计报告负责，不把前端携参测试当作端到端 CAS 验证。

## 重点负向验证：未发现应当误报的严重故障

- **自绘标题栏：** 通过真实 close 按钮验证有 runner.jobs 时拒绝关闭并提示；任务清空后可关闭；最大化/还原按钮能切换 Qt 状态。原生边缘拖拽与高 DPI 仍需 Windows 可视操作，不以 offscreen 结果冒充通过。
- **管理动作表单：** 必填空值、2026-02-30 无效日期均被拒绝；完整有效日期可通过。后台对动作含义/权限的校验不由本 GUI 测试代替。
- **AI 转任务：** 入口在记录编辑 → AI辅助填写 → 成功有 call_id 和 suggestions 后启用“选择管理建议，建立正式任务”；AI 工作室的普通草稿则是手工新建任务，不承诺自动拆解。任务表默认不勾选，最多20条，提交前再次确认；请求处理中 reject 被阻止；现有请求指纹用于相同内容重试。
- **配置默认/历史：** 切换模型平台只更改提示与 placeholder，不隐式覆盖已有地址/模型/密钥引用；“使用此平台默认值”才覆盖。提示词历史是全库历史，下拉选择版本并复制为新提示词，不是原位回滚。
- **导入导出：** 完整导出保存的是客户端选定本地路径；服务器备份只显示服务器路径，并非本地下载。Excel 根据后缀选择格式；表格列值优先于备份 JSON；导入先预览后确认，重复 ID 不覆盖更新。现有 GUI/client 测试覆盖 JSON/Excel 回读及文本公式防护。
- **专项入口：** 在导航“项目规划”分组内，不在“日常工作”；配置式模板位于专项页的第二个 tab。内置与自定义工作空间使用模板快照，不追随模板后续修改。

## 手册限制 / 用户应知行为（不列为严重缺陷）

1. 日常检查草稿仅进程内保留，切页保留但退出不保存；切换检查模板会重置当前检查项。界面已明确退出不保存，手册应强调模板切换前核对草稿。
2. AI 的取消/切页不等于取消已发出的模型计费；关闭主窗在任务结束前会被阻止。发送结果不确定应先核对平台和发送记录。
3. AI 建议转任务创建的是待执行任务，不发送通知、不构成审批；最多20项且需要人工核实负责人、期限和验收条件。
4. 模型平台切换不自动更换默认值是防止覆盖输入的设计，并非切换失效。
5. 提示词历史复制可能继承旧版 `is_default`；提交前应核对默认选项，不能把“复制”误当成无副作用的历史查看。
6. 专项仅用于资料、证据与整改跟踪，IPO 专项不是审计/法律意见或上市合规保证。
7. offscreen 不覆盖原生 Windows 多屏、125%/150% 缩放、边缘系统缩放、标题栏拖拽。没有观察证据前不将其泛列为产品故障。

## 执行证据

- 修复前独立审计：6 项全部通过；其中4项断言观察到的缺陷，另2项验证标题栏与管理校验。
- 修复前已有桌面回归：`62 passed in 32.02s`。说明上述异步失效路径确实是原套件未覆盖的情况，而非已有测试失败的重复报告。
- 第一轮修复：审计 + 检查 + 专项 `27 passed in 3.86s`。
- 最终全部桌面回归（含9项新增正式测试）：`71 passed in 36.79s`。
- 正式收集入口：[test_gui_audit_regressions.py](../tests/test_gui_audit_regressions.py)，复用 [audit_gui_repro.py](../tests/audit_gui_repro.py) 的隔离场景并加入模板版本携参。

执行命令：

```powershell
$env:QT_QPA_PLATFORM='offscreen'
python -m pytest tests/test_gui_audit_regressions.py tests/test_gui.py tests/test_gui_integration.py tests/test_ui_async_v2.py tests/test_ai_form_ui.py tests/test_checklists_ui.py tests/test_workspace_ui.py tests/test_workspaces_ui.py -q
```

运行环境出现 Qt offscreen 字体目录/propagateSizeHints 警告及 requests 依赖版本警告；没有把它们归类为 GUI 业务失败。
