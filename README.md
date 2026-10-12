# IT推进管理工具 v0.9.0

面向**单可信IT管理员**的 PySide6 Windows 桌面与统一 Python/FastAPI 服务端。覆盖规划、项目、任务、人员考核证据、运营治理台账、提醒与体检、证据附件、专项工作空间、事件到变更链条、统计看板、汇报审核、通知、AI及日周月调度。**交付源码，不编译EXE或CHM。**

## 快速开始

1. 安装 Python 3.11 或更新版本，将完整源码放在可写目录。
2. 运行 [setup_env.bat](<setup_env.bat>) 安装虚拟环境依赖。
3. 运行 [start_local.bat](<start_local.bat>)，启动/复用本机服务并打开桌面。
4. 使用虚构数据验证流程，再配置授权测试群、邮箱和AI账号。
5. 点击桌面“帮助”或直接打开 [HTML使用手册](<manual/index.html>)。

| 入口 | 用途 |
|---|---|
| [setup_env.bat](<setup_env.bat>) | 安装与检查依赖 |
| [start_local.bat](<start_local.bat>) | 本机服务＋桌面 |
| [start_server.bat](<start_server.bat>) | 仅服务，开启调度 |
| [start_desktop.bat](<start_desktop.bat>) | 仅桌面，支持远程参数 |
| [run_tests.bat](<run_tests.bat>) | 无界面自动测试 |

默认服务为 `http://127.0.0.1:18765`，本机启动器在源码下 `data` 保存SQLite与随机管理员令牌。关闭桌面**不会**停止服务；停止服务控制台后周期任务**不执行**。默认使用本地SQLite，不需要另装数据库。

远程客户端示例（域名和路径须替换）：

```bat
start_desktop.bat --url https://itm.example.invalid --token-file "C:\Private\admin.token"
```

远程必须经受控HTTPS入口，服务保持单实例；BAT不会自动配置反向代理或生产托管。第三方凭据只在服务器环境变量或Windows DPAPI库保存，配置表仅保存引用；详见 [凭据与安全](<manual/security.html>)。

## 主要能力

- 19类业务台账、专项工作空间、任务推进、日常检查与整改闭环。
- 程序内提醒、可选系统托盘提醒（默认关闭、限频、安静时段）、台账体检与处理状态。
- 证据附件受控保存与SHA-256校验；数据库＋附件＋可选DPAPI凭据密文的完整备份包。
- 全局搜索、批量修改与导出、服务端分页、图表下钻与深色/字号偏好。
- AI辅助填表、周报汇总、会议纪要转任务；所有写库/发送均需人工确认。

## 手册与设计

- [安装启动](<manual/install.html>)、[本地与远程架构](<manual/architecture.html>)
- [规划任务](<manual/planning.html>)、[治理台账](<manual/ledgers.html>)、[人员考核证据流程](<manual/people.html>)
- [汇报审核发送](<manual/reports.html>)、[三平台Webhook及SMTP字段](<manual/notifications.html>)
- [AI平台与提示词变量/版本](<manual/ai.html>)、[调度漏跑与不确定重发](<manual/schedules.html>)
- [Excel/JSON导入导出](<manual/data.html>)、[备份离线恢复](<manual/backup.html>)、[FAQ与限制](<manual/faq.html>)
- [CHM工程及中文目录兼容策略](<manual/chm.html>)：提供 [HHP](<manual/itmanager.hhp>)、[HHC](<manual/itmanager.hhc>)、[HHK](<manual/itmanager.hhk>) 源文件，**未编译CHM**。
- [设计文档索引](<docs/README.md>) 为设计基线，不代表每项设计均已实现；实际实现与验证记录见 [更新日志](<更新.md>)。

手册共15个独立主题页面，UTF-8、本地CSS、相对链接，无CDN；直接用浏览器离线阅读。内容源为 [手册生成器](<manual/build_manual.py>)，编辑后执行：

```bat
.venv\Scripts\python.exe manual\build_manual.py
.venv\Scripts\python.exe -m pytest tests\test_manual.py -q
```

## 数据保护与恢复

在“系统与导入导出”执行服务端一致性备份。业务JSON/Excel导出**不能替代数据库备份**，不包含完整配置、草稿、发送和审计。重复ID导入默认拒绝覆盖。

离线恢复前必须停止服务、确认目标路径、保留当前数据，并核对已发送结果：

```bat
.venv\Scripts\python.exe -m itmanager.restore "data\backups\实际备份.sqlite3" --target "data\itmanager.sqlite3" --confirm
```

恢复工具校验备份、保留恢复前副本，并停用恢复库中的周期计划；核对后人工启用。DPAPI绑定Windows服务账号及系统密钥条件，跨机迁移可能需重配凭据。完整操作见 [备份恢复](<manual/backup.html>)。

## 不应误解的边界

- **无员工登录、多人权限隔离或员工在线签名**；自评和反馈由管理员代录并注明来源。
- **其他数据库只有适配口**，未提供可用PostgreSQL/MySQL后端。
- **真实外部平台尚未联调，生产常开服务器尚未部署验证**；mock测试不等于真实发信/AI成功。
- SMTP**仅发送**，不收取邮件；机器人不是企业通讯录或个人消息系统。
- AI仅生成待审草稿/建议，不自动创建任务、执行操作或决定绩效等级。
- 服务关闭、系统休眠/断电时不执行周期；恢复最多处理一个漏跑槽位，不补发全部历史。
- 网络超时可能已被平台接收；不确定结果必须人工核对后重发，不能保证跨第三方“恰好一次”。
- 默认单实例SQLite本地磁盘；不支持共享盘多主写入、分布式调度或离线同步。
- 完整业务导出和数据库含人员、考核及权限资料，须按敏感信息保护；不得把真实令牌/API Key/Webhook/邮箱密码提交到版本库。

已按源码对齐Excel列优先/原模板映射、业务历史入口及考核定稿校验；最终GUI交互仍需人工发布前核验，详见 [限制与待核验清单](<manual/faq.html>)。

---

## 许可证

本项目采用 [MIT 许可证](LICENSE)。

## 第三方与上游声明

本项目包含或依赖第三方组件，其版权与许可证归各自作者所有，
详见仓库内的依赖清单与相关声明文件。若本项目基于其他开源项目二次开发，
已在正文中注明上游项目与所基于的版本，本项目为**非官方**衍生作品。

## 现状与限制

- 本项目为个人开发项目，按“现状”提供，不附带任何明示或暗示的担保。
- README 与代码注释中记录的验证结论仅针对当时的具体环境与版本，不代表在其他环境下同样成立。
- 涉及本地凭据的部分统一使用系统凭据库或环境变量，仓库内不含真实密钥；
  示例配置仅使用占位符。
