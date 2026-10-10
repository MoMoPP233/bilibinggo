# Binggo V118 P1 审计修复报告

核对日期：2026-10-10。项目 `E:\bilibinggo`；分支 `fix/v118-audit-p1-safety`；基线 HEAD `6d1a8966585fc558f5bf66d0dbef702c0c8cb39f`（已提交 V117）。执行前 worktree clean；本轮源码、测试和文档为预期未提交修改。没有 commit、push、打包、结束真实进程或修改真实 Profile。

## 1. 逐项结论

| 疑点 | 根因 | 修改文件 | 新增测试 | 状态 |
| --- | --- | --- | --- | --- |
| D-001 | installer 按名强杀全部 Binggo，Restart Manager 可强制关闭应用 | `packaging/windows/installer.iss` | `test_audit_p1_v118.py`：无 kill/自动重启，0/2/4/残留 service 进程计数策略及检测失败保守阻挡 | 已修复并通过离线测试 |
| D-002 | frozen 的 `sys.executable` 是 Binggo.exe，不支持 Python `-c` 初始化契约 | `binggo_launcher.py`、`src/login_onboarding.py`、新增 `src/profile_db_init.py` | `test_audit_p1_v118.py`：模拟 frozen 入口、真实临时 DB 初始化、父 runtime 不变、失败不发布、已有 DB 不覆盖 | 已修复并通过离线测试 |
| D-004 | Winner 昵称补读把 formal risk 当可忽略的补充信息失败 | `src/winner_scan/service.py` | `test_audit_p1_v118.py`：-352/-509/HTTP429 传播，候选保留，后续请求停止，不提交成功 checkpoint | 已修复并通过离线测试 |
| D-007 | 五个旧维护脚本有仓库级固定路径或广义异常后继续请求 | `scripts/backfill_missing_details.py`、`rebuild_all_activity_details.py`、`retry_failed_rebuild.py`、`discover_watch_users.py`、`replenish_watch_users.py` | `test_maintenance_safety_v118.py`：两 Profile 路径/输入隔离，风险停止，先前有效结果保留，-315/-412 不升格 | 已修复并通过离线测试 |
| D-018 | 人工例外只依赖历史评估与额外声明，缺当前评估复核；产品边界需用户决定 | `src/repost_cleanup.py`、既有 `tests/test_repost_cleanup_service.py` | `test_manual_delete_safety_v118.py`：fresh blocked/excluded/deferred 零 POST，manual/safe 保留显式授权，身份变化阻挡，仅删除本人 repost ID | 已修复并通过离线测试 |
| D-021 | 恢复探测未使用普通评论 durable pending；formal 可被归入 unknown；breaker 清除会清掉排除证据 | `src/comment_breaker.py`、`comment_confirmation.py`、`comment_probe.py`、`lottery_actions.py` | `test_audit_p1_v118.py`：A 超时→B 成功清 breaker→A 普通参与仍零重发，落盘/崩溃/12014/UID/legacy/formal，以及终态写盘失败仍传播 formal | 已修复并通过离线测试 |
| 专项：频繁转发 UNKNOWN | 本地 mock 覆盖了响应不明路径，但不能说明真实账号频发的具体原因 | 未修改 repost 实现或其 guard | `test_repost_unknown_investigation_v118.py`：14 cases，实际 HTTP/JSON 处理与 mock transport | 暂未复现 |

六项 P1 的代码修复/安全补强已完成；原 21 项中其余 15 项尚未完成，不因全量测试通过标记关闭。额外的真实频繁转发 UNKNOWN 原因仍未解决。D-018 用户已经明确选择“保留人工例外及二次确认（现有行为）”，不是尚待决策，也不是自动删除授权。

## 2. 必须保留的边界

### 安装与新 Profile

安装器不再请求关闭/杀死账号实例。WMI 检测任何 `Binggo.exe` 时阻止安装/卸载，无法检测同样阻挡；开始安装前再次检查。用户应先从每个托盘安全退出，不在检查后重新启动旧实例。离线验证是**静态 Pascal 策略模拟**，没有执行真实 Inno Setup 或 WMI，也没有强杀多实例。源码 `python.exe` 不是这个映像名检测的对象；本修复不能替代用户确认共用数据根的源码实例也已安全退出。检测与文件覆盖之间不提供全局原子启动锁，安装期间不要启动实例。

新 Profile 仅在专用子进程初始化 unpublished staging DB；source 用 `python -m src.profile_db_init`，frozen 用正式 launcher 的隐藏 `--init-profile-db`。入口在选择 runtime/获取实例锁前分流，禁止与运行参数混用。严格 staging 名称/绝对路径检查，不覆盖已存在 DB，不热切换父服务引擎。初始化成功且只读核对 Schema 12 后才原子发布；失败由原有 staging 清理处理。父进程 SQLite 检查句柄及时关闭，避免 Windows 重命名失败。没有 Schema 迁移。模拟 frozen 通过不等于真实新安装包已验收。

### 正式风控与维护 CLI

正式风控仅 `-352`、`-509`、HTTP 429；没有把 `-315/-412` 或网络失败扩展成 formal risk。Winner 昵称失败遇到 formal 立即传播至上层，已发现候选与通知准备只落本地，不 dispatch 通知，不继续来源请求，不前移未完成来源 checkpoint。普通昵称查询失败仍使用 UID 回退，不额外补请求。

五个维护 CLI 使用当前冻结 Profile 路径，详情重建在 formal 退出前保存已完成结果并标 `complete=False`；本地写失败也不能吞掉原风险。CLI 独立运行时会终止自身请求，不宣称能向另一个运行中的 Scheduler 发送暂停通知。三个详情写工具及 replenish 保留原有写锁；discover 仍是只读候选发现/JSON 导出，不取得 Profile WriterLease，不修改 live WATCH DB。既有参数和显式 `--output` 保留；不会自动将不明归属的旧仓库 JSON 分配给账号，旧导出若要使用须用户核实后显式选择。

### 人工 DELETE 例外

官方 safe 删除仍遵循严格官方开奖/中奖条件。manual_review 仍须额外二次确认；V118 不取消用户接受的人工风险，也不将其升级为 safe。删除前重读本人顶层 FORWARD、top author/UID、repost/original 精确关系，重新评估；人工路径仅 fresh manual_review/safe 可继续，fresh blocked/excluded/deferred 不允许。POST 前再次核对 runtime Profile、DB 路径和登录 UID；先持久 `delete_pending`，只提交 `dyn_id_str=repost_dynamic_id`，`retries=0`。原 dynamic ID 从不作为 DELETE 目标。UNKNOWN/pending 不自动再删。人工路径无法保证未中奖/无需领奖，复核不是这样的保证。

### 评论探测防重与旧数据

probe 是真实评论 mutation，同样先 durable pending，写失败零 POST；可靠 code=0/12051 后 confirmed，超时、断连、响应不明 unknown，若 unknown 落盘失败原 pending 仍阻挡。12014 是明确拒绝，记 failed 并沿用一小时恢复节奏；正式风险继续传播，不允许换目标绕过暂停。即使正式风险响应后的终态 guard 写盘失败，也保留原风险传播和已落盘的 pending。正常评论、人工恢复、UID/Profile 动作锁与零重试契约保留。

新探测 guard 按 UID/目标存储，breaker 清除保留探测证据；没有可靠 UID 的旧排除项按整个 Profile 保守阻挡，不猜归属。已有明确 confirmed 正向证据仍跳过不重复 POST。**旧版本已经清掉的排除证据无法重建**，不从标题/时间/当前登录猜回填；该历史残余不能声称已永久覆盖。新增 JSON 字段向后可读取，但旧程序不认识修复契约，不能降回旧版本对同一 Profile 继续参与。

### 频繁转发 UNKNOWN

实际 `BilibiliClient.post_form` 的 mock HTTP 路径：严格整数 code=0 确认成功；bool/字符串 code、缺 code、非 JSON、HTTP 500、正式风控及连接/协议/超时分别验证现有保守处理。再次自动调用不会再次 POST。没有修改 repost guard，没有增加自动重试，没有把异常推断成“肯定未执行”。真实根因还需不含凭据的 HTTP 状态、业务码或异常类别；网络结果不明不能通过自动重新转发修复。

## 3. 离线测试与 Gate

新增四份 V118 测试共 70 cases；加既有 discover 无写锁契约，核心专项共 71 cases。新增测试均使用 mock、临时根/临时 DB；项目默认外部 socket/DNS guard 阻止真实网络。未接真实服务、未修改 `E:\profiles`。

```text
python -m pytest -q tests/test_operational_polish_v10.py::test_readonly_cli_discover_watch_users_has_no_lease tests/test_audit_p1_v118.py tests/test_maintenance_safety_v118.py tests/test_manual_delete_safety_v118.py tests/test_repost_unknown_investigation_v118.py --tb=short
python -m pytest -q --tb=short
python -m compileall -q src web tests
# web/frontend 下：
npm run test
npm run typecheck
npm run build
# 仓库根目录：
git diff --check
```

| Gate | 最终结果 |
| --- | --- |
| V118 专项及既有只读 CLI 契约 | 71 passed；含评论/formal 回归的扩展定向 160 passed |
| 完整 Python | 2742 passed、2 skipped、0 failed；330.50 秒 |
| Vitest | 277 passed / 31 files |
| Typecheck | PASS，exit 0 |
| 前端 production build | PASS，exit 0 |
| compileall | PASS，exit 0 |
| git diff --check | PASS；仅 LF/CRLF 转换提示，无空白错误 |

首次完整 Python：2734 passed、2 skipped、1 failed，347.45 秒。失败为新增 discover WriterLease 与既有“只读导出不加 Profile 写锁”契约冲突；撤回不必要的锁，没有删除安全断言，未改变 Profile 路径/formal 修复，再完整重跑。

最终代码复核追加三项正式风险+终态写盘失败测试：修复前 -352/-509 两项未传播（失败）、HTTP429 通过；已补最小处理以防磁盘错误盖掉 payload 风控，随后专项及最终全量通过。之前 2739 passed 的全量结果不冒充最后代码的 Gate。

最终两项 skipped：`test_onboarding_secret_staging_v31.py::test_secret_sidecar_not_world_readable`（POSIX only）和 `test_secure_files.py::test_harden_permissions_posix`（POSIX chmod only）；Windows 平台不适用。核对这些文件单跑为 21 passed、2 skipped。不是跳过 P1 安全断言。

先前扩展定向 639 passed、1 failed；失败是 V101 fixture 期间 runtime root 发生变化，隔离重跑该 case 1 passed，属于顺序/并发敏感现象，未为此修改 Scheduler。全量最终结果以本表为准。现有 FastAPI/httpx deprecation warning 及 Vite 同模块静态/动态 import 提示非 Gate 失败。

## 4. 交付与后续

修改文件（仓库相对路径）：

- 生产代码/脚本：`binggo_launcher.py`、`packaging/windows/installer.iss`、`scripts/backfill_missing_details.py`、`scripts/discover_watch_users.py`、`scripts/rebuild_all_activity_details.py`、`scripts/replenish_watch_users.py`、`scripts/retry_failed_rebuild.py`、`src/comment_breaker.py`、`src/comment_confirmation.py`、`src/comment_probe.py`、`src/login_onboarding.py`、`src/lottery_actions.py`、`src/repost_cleanup.py`、`src/winner_scan/service.py`；新增 `src/profile_db_init.py`。
- 测试：修改 `tests/test_repost_cleanup_service.py`；新增 `tests/test_audit_p1_v118.py`、`tests/test_maintenance_safety_v118.py`、`tests/test_manual_delete_safety_v118.py`、`tests/test_repost_unknown_investigation_v118.py`。
- 文档：`项目维护文档.md`、`docs/audits/PROJECT_FUNCTION_INVENTORY.md`、`docs/audits/PROJECT_CODE_COVERAGE.md`、`docs/audits/PROJECT_FEATURE_TEST_PLAN.md`；新增本文 `docs/audits/V118_P1_FIX_REPORT.md`。

- 可以审核本轮源码/测试差异；全部 Gate 通过后由用户决定是否提交，不自动 commit/push。
- Windows 安装阻挡策略、真实 frozen 新账号创建需要后续授权的独立沙盒人工验收；本轮没有打包，旧 V117 安装包不包含这些修复。
- 不要在真实账号用 probe、参与或 DELETE 验收本轮；先复用离线 fixture/临时 Profile。
- 未改前端业务、SQLite Schema、Profile 隔离、Cookie、Scheduler、DS3、Following/Pipeline 分类或 BILI_RPS；前端 build 仅产生忽略的构建文件。
- V119 建议优先核实 D-003 Winner 500 条保留方向、D-005 native build 失败/旧产物识别、D-017 CLI 与 Web checkpoint 契约。其余审计项仍保留原编号，不自动开发下一版。

最终差异为 25 个预期文件（19 个 tracked 修改、6 个新增未跟踪文件）；普通 `git diff --stat` 不包含新增文件，不能据其 19 文件数宣称新增测试已暂存。未执行 git add。详细修改以 `git status --short`/`git diff` 和新增文件为准；本报告不把静态策略测试写成安装版真实验收，不把频繁 UNKNOWN 暂未复现写成已修复。
