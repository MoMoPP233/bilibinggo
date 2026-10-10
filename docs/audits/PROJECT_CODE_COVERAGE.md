# Binggo 自有源码与测试覆盖清单

> V118 增量更新（2026-10-10）：本表的 508 文件、行号和静态阅读统计保留为 V117 审计快照，不冒充当前执行覆盖率。V118 新增 `src/profile_db_init.py` 与四份专项测试，修改范围、已执行 Gate 和 D-001/002/004/007/018/021 状态见 [V118 P1 修复报告](V118_P1_FIX_REPORT.md)；未重新开展全项目普查。

## 审计基线与统计口径

2026-10-09～10 静态只读审计；分支 `release/final-gate-package-v117`；HEAD `826cde719154765dc09cb2b116e52fbaa2787a58`（V116错峰范围修正）。本轮只创建/更新三份审计Markdown。用户已有未跟踪 `项目维护文档.md` 保持不变，不纳入业务源码。没有运行pytest/Vitest/build，也没有访问真实B站/LLM、本地业务HTTP或真实Profile数据。

递归枚举Git跟踪的自有可执行源码、脚本、样式、页面及构建/CI配置：`.py/.ts/.js/.css/.html/.ps1/.sh/.spec/.iss/.yml/.yaml/.toml`。第三方node_modules/虚拟环境、dist/build、二进制图片/字体/可执行产物不作为自有逻辑代码；Markdown历史资料不代替源码读取。额外核对27个JSON/依赖/文本/SVG/XML及示例/仓库配置，单列不混入508统计。未跟踪可执行自有源码：未发现；本轮报告与用户Markdown是未跟踪文档。

- 文件总数：508；已检查：508；尚未检查：0；共129,323行（含测试/样式/配置，不是业务LOC）。
- Python426文件，AST索引5577个函数/异步函数（含方法、内嵌函数和测试辅助），409个类；已解析函数体并结合调用者/状态分批阅读。
- 其中Python测试225个 `test_*.py` + 4个包/fixture辅助文件；2107个静态test函数不等于pytest参数化case数，更不等于本轮passed。
- TypeScript65文件，包括31个Vitest `.test.ts`、Playwright smoke/helpers及配置，余为生产模块；未发现Vue组件。
- FastAPI显式方法/路径90项（含PUT/POST别名及条件E2E两项）；MCP34工具；主HTML76个button/input/select/textarea控件；动态列表按钮另在功能/入口索引。
- 识别功能137项：SC23、ACT18、JOB12、UI17、CLN10、NTF6、SYS27、OPS24。数量按用户能力/独立契约归并，不按函数数、重复按钮或版本数计。
- 当前用户可见/常规生产调用不能确认的4组链：ACT-014旧单动作函数、CLN-010内部cycle、UI-016旧查更新按钮绑定、NTF-006四个预留事件生产者。各有内部函数/兼容API或测试证据，不等于全部废弃。OPS-017明确CLI入口存在，但属于历史迁移工具、非日常构建。

覆盖含义：**文件级静态实现阅读覆盖，不是分支执行覆盖/自动测试通过/真实安装验收**。Python AST/TS与HTML静态索引只用于防遗漏，结论同时阅读了函数体、事件绑定、异常分支、JSON/SQLite状态写入和相关断言；动态执行、平台API和未运行测试标明待核实。

## 模块统计

| 范围 | 文件 | 行数 | 主要功能 |
|---|---:|---:|---|
| .github | 5 | 283 | OPS-024 |
| 根目录 | 1 | 709 | SYS-009～013 |
| config | 1 | 48 | SC-001～007 |
| docs | 3 | 5317 | SYS-025 |
| mcp | 8 | 1098 | OPS-019～023 |
| packaging | 9 | 714 | SYS-023～024、OPS-018 |
| scripts | 34 | 3934 | OPS-001～018、SYS-004/009/022 |
| src | 122 | 34106 | SC、ACT、CLN、NTF及数据/运行基础设施 |
| tests | 229 | 48354 | 后端/CLI安全、迁移、mock异常断言 |
| web | 96 | 34760 | API、Scheduler/Runner、TS界面及前端测试 |

## 逐文件覆盖（508行）

“疑点”链接功能清单D编号；测试未执行，不能以未发现独立疑点宣称无Bug。关联编号为已核实模块/断言关系索引，不表示一个测试覆盖对应功能所有路径。依赖初始化/包文件也列出，不按文件名假造业务功能。

| 文件路径 | 所属模块/行数 | 是否已读取实现 | 关联功能编号 | 是否存在疑点/验证边界 |
|---|---|---|---|---|
| `.github/workflows/ci.yml` | .github；118行 | 是；配置/脚本执行与资源边界已读 | OPS-024 | 未发现独立疑点；运行行为未在本轮验证 |
| `.github/workflows/pages.yml` | .github；34行 | 是；配置/脚本执行与资源边界已读 | OPS-024 | 未发现独立疑点；运行行为未在本轮验证 |
| `.github/workflows/release-macos.yml` | .github；50行 | 是；配置/脚本执行与资源边界已读 | OPS-024 | 未发现独立疑点；运行行为未在本轮验证 |
| `.github/workflows/release-windows.yml` | .github；54行 | 是；配置/脚本执行与资源边界已读 | OPS-024 | 未发现独立疑点；运行行为未在本轮验证 |
| `.github/workflows/star-history.yml` | .github；27行 | 是；配置/脚本执行与资源边界已读 | OPS-024 | 未发现独立疑点；运行行为未在本轮验证 |
| `binggo_launcher.py` | binggo_launcher.py；709行 | 是；实现/调用/条件/持久状态已读 | SYS-009、SYS-011、SYS-012、SYS-013 | D-002、D-016 |
| `config/sources.yaml` | config；48行 | 是；配置/脚本执行与资源边界已读 | SC-001、SC-002、SC-003、SC-004、SC-005、SC-006、SC-007 | 未发现独立疑点；运行行为未在本轮验证 |
| `docs/index.html` | docs；775行 | 是；布局/交互状态及使用关系已读 | SYS-025 | 未发现独立疑点；运行行为未在本轮验证 |
| `docs/landing.css` | docs；3011行 | 是；布局/交互状态及使用关系已读 | SYS-025 | 未发现独立疑点；运行行为未在本轮验证 |
| `docs/landing.js` | docs；1531行 | 是；实现/调用/条件/持久状态已读 | SYS-025 | 未发现独立疑点；运行行为未在本轮验证 |
| `mcp/binggo_mcp/__init__.py` | mcp；3行 | 是；包初始化/导出已读 | OPS-019 | 未发现独立疑点；运行行为未在本轮验证 |
| `mcp/binggo_mcp/__main__.py` | mcp；4行 | 是；实现/调用/条件/持久状态已读 | OPS-019 | 未发现独立疑点；运行行为未在本轮验证 |
| `mcp/binggo_mcp/client.py` | mcp；125行 | 是；实现/调用/条件/持久状态已读 | OPS-019 | D-006 |
| `mcp/binggo_mcp/instructions.py` | mcp；57行 | 是；实现/调用/条件/持久状态已读 | OPS-023 | 未发现独立疑点；运行行为未在本轮验证 |
| `mcp/binggo_mcp/jobs.py` | mcp；178行 | 是；实现/调用/条件/持久状态已读 | OPS-020、OPS-021、OPS-023 | 未发现独立疑点；运行行为未在本轮验证 |
| `mcp/binggo_mcp/serial.py` | mcp；22行 | 是；实现/调用/条件/持久状态已读 | OPS-023 | 未发现独立疑点；运行行为未在本轮验证 |
| `mcp/binggo_mcp/server.py` | mcp；688行 | 是；实现/调用/条件/持久状态已读 | OPS-019、OPS-020、OPS-021、OPS-022 | 未发现独立疑点；运行行为未在本轮验证 |
| `mcp/pyproject.toml` | mcp；21行 | 是；配置/脚本执行与资源边界已读 | OPS-019 | 未发现独立疑点；运行行为未在本轮验证 |
| `packaging/icon_raster.py` | packaging；16行 | 是；实现/调用/条件/持久状态已读 | SYS-024、OPS-018 | 未发现独立疑点；运行行为未在本轮验证 |
| `packaging/macos/binggo.spec` | packaging；131行 | 是；配置/脚本执行与资源边界已读 | SYS-024 | 未发现独立疑点；运行行为未在本轮验证 |
| `packaging/macos/build.py` | packaging；154行 | 是；实现/调用/条件/持久状态已读 | SYS-024 | 未发现独立疑点；运行行为未在本轮验证 |
| `packaging/macos/build.sh` | packaging；5行 | 是；配置/脚本执行与资源边界已读 | SYS-024 | 未发现独立疑点；运行行为未在本轮验证 |
| `packaging/macos/generate_icns.py` | packaging；65行 | 是；实现/调用/条件/持久状态已读 | OPS-018 | 未发现独立疑点；运行行为未在本轮验证 |
| `packaging/windows/binggo.spec` | packaging；115行 | 是；配置/脚本执行与资源边界已读 | SYS-023 | D-016 |
| `packaging/windows/build.ps1` | packaging；116行 | 是；配置/脚本执行与资源边界已读 | SYS-023 | D-005 |
| `packaging/windows/generate_icon.py` | packaging；37行 | 是；实现/调用/条件/持久状态已读 | OPS-018 | 未发现独立疑点；运行行为未在本轮验证 |
| `packaging/windows/installer.iss` | packaging；75行 | 是；配置/脚本执行与资源边界已读 | SYS-023 | D-001 |
| `scripts/audit_charging_activities.py` | scripts；77行 | 是；实现/调用/条件/持久状态已读 | OPS-009 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/backfill_missing_details.py` | scripts；112行 | 是；实现/调用/条件/持久状态已读 | OPS-006 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/backfill_missing_lottery_times.py` | scripts；45行 | 是；实现/调用/条件/持久状态已读 | OPS-010 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/bili_login.py` | scripts；40行 | 是；实现/调用/条件/持久状态已读 | SYS-004 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/check_ds1.py` | scripts；25行 | 是；实现/调用/条件/持久状态已读 | OPS-001 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/check_ds2.py` | scripts；25行 | 是；实现/调用/条件/持久状态已读 | OPS-001 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/check_ds3.py` | scripts；25行 | 是；实现/调用/条件/持久状态已读 | OPS-001 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/check_ds4.py` | scripts；25行 | 是；实现/调用/条件/持久状态已读 | OPS-001 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/check_ds5.py` | scripts；25行 | 是；实现/调用/条件/持久状态已读 | OPS-001 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/check_ds6.py` | scripts；25行 | 是；实现/调用/条件/持久状态已读 | OPS-001 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/check_ds7.py` | scripts；25行 | 是；实现/调用/条件/持久状态已读 | OPS-001 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/check_messages.py` | scripts；136行 | 是；实现/调用/条件/持久状态已读 | OPS-003 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/discover_watch_users.py` | scripts；191行 | 是；实现/调用/条件/持久状态已读 | OPS-011 | D-007 |
| `scripts/export_activities_seed.py` | scripts；61行 | 是；实现/调用/条件/持久状态已读 | OPS-013 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/export_migrate_bundle.py` | scripts；251行 | 是；实现/调用/条件/持久状态已读 | OPS-015 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/export_private_complete.py` | scripts；217行 | 是；实现/调用/条件/持久状态已读 | OPS-015 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/export_private_ready.py` | scripts；274行 | 是；实现/调用/条件/持久状态已读 | OPS-015 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/export_state_seed.py` | scripts；55行 | 是；实现/调用/条件/持久状态已读 | OPS-013 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/fetch_activity_info.py` | scripts；49行 | 是；实现/调用/条件/持久状态已读 | OPS-004 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/handtest_data_layer.py` | scripts；139行 | 是；实现/调用/条件/持久状态已读 | OPS-016 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/import_json_to_db.py` | scripts；93行 | 是；实现/调用/条件/持久状态已读 | OPS-014 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/maintain_local_activities.py` | scripts；177行 | 是；实现/调用/条件/持久状态已读 | OPS-005 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/migrate_frontend_app.py` | scripts；814行 | 是；实现/调用/条件/持久状态已读 | OPS-017 | D-012 |
| `scripts/migrate_lottery_times.py` | scripts；64行 | 是；实现/调用/条件/持久状态已读 | OPS-010 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/participate.py` | scripts；91行 | 是；实现/调用/条件/持久状态已读 | OPS-002 | D-006 |
| `scripts/purge_all_dead_links.py` | scripts；57行 | 是；实现/调用/条件/持久状态已读 | OPS-008 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/rebuild_all_activity_details.py` | scripts；133行 | 是；实现/调用/条件/持久状态已读 | OPS-007 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/render_brand_png.py` | scripts；39行 | 是；实现/调用/条件/持久状态已读 | OPS-018 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/replenish_watch_users.py` | scripts；204行 | 是；实现/调用/条件/持久状态已读 | OPS-011 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/retry_failed_rebuild.py` | scripts；112行 | 是；实现/调用/条件/持久状态已读 | OPS-007 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/rollback_ds_containers.py` | scripts；115行 | 是；实现/调用/条件/持久状态已读 | OPS-012 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/run_dashboard.py` | scripts；81行 | 是；实现/调用/条件/持久状态已读 | JOB-009、SYS-009 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/run_e2e_server.py` | scripts；92行 | 是；实现/调用/条件/持久状态已读 | SYS-022 | 未发现独立疑点；运行行为未在本轮验证 |
| `scripts/sync_brand_icons.py` | scripts；40行 | 是；实现/调用/条件/持久状态已读 | OPS-018 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/__init__.py` | src；0行 | 是；包初始化/导出已读 | SYS-001 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/activity_failures.py` | src；81行 | 是；实现/调用/条件/持久状态已读 | UI-003、SYS-019 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/activity_seed.py` | src；92行 | 是；实现/调用/条件/持久状态已读 | SYS-016 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/activity_status.py` | src；55行 | 是；实现/调用/条件/持久状态已读 | SC-023 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/activity_store.py` | src；260行 | 是；实现/调用/条件/持久状态已读 | SC-022 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/app_logging.py` | src；279行 | 是；实现/调用/条件/持久状态已读 | SYS-020 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/app_paths.py` | src；159行 | 是；实现/调用/条件/持久状态已读 | SYS-001、SYS-017 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/bilibili_auth.py` | src；37行 | 是；实现/调用/条件/持久状态已读 | ACT-011、SYS-004、SYS-026 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/bilibili_client.py` | src；634行 | 是；实现/调用/条件/持久状态已读 | ACT-018、JOB-011、JOB-012、SYS-026 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/bilibili_login.py` | src；588行 | 是；实现/调用/条件/持久状态已读 | SYS-004 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/bilibili_rate_limit.py` | src；130行 | 是；实现/调用/条件/持久状态已读 | JOB-011 | D-011 |
| `src/bilibili_request_budget.py` | src；413行 | 是；实现/调用/条件/持久状态已读 | JOB-011 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/canonical_identity.py` | src；168行 | 是；实现/调用/条件/持久状态已读 | SC-019 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/check_cli.py` | src；64行 | 是；实现/调用/条件/持久状态已读 | OPS-001 | D-017 |
| `src/comment_breaker.py` | src；374行 | 是；实现/调用/条件/持久状态已读 | ACT-017 |未发现独立疑点；运行行为未在本轮验证、D-021 |
| `src/comment_confirmation.py` | src；151行 | 是；实现/调用/条件/持久状态已读 | ACT-008 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/comment_probe.py` | src；94行 | 是；实现/调用/条件/持久状态已读 | ACT-017 |未发现独立疑点；运行行为未在本轮验证、D-021 |
| `src/config_health.py` | src；207行 | 是；实现/调用/条件/持久状态已读 | UI-016 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/config_validate.py` | src；65行 | 是；实现/调用/条件/持久状态已读 | SYS-026、UI-014 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/dashboard_server.py` | src；86行 | 是；实现/调用/条件/持久状态已读 | SYS-001 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/data_paths.py` | src；698行 | 是；实现/调用/条件/持久状态已读 | SYS-001、SYS-017 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/db/__init__.py` | src；15行 | 是；包初始化/导出已读 | SYS-014 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/db/activity_codec.py` | src；139行 | 是；实现/调用/条件/持久状态已读 | SYS-015 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/db/engine.py` | src；74行 | 是；实现/调用/条件/持久状态已读 | SYS-014 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/db/import_json.py` | src；992行 | 是；实现/调用/条件/持久状态已读 | SYS-016、OPS-014 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/db/json_cols.py` | src；17行 | 是；实现/调用/条件/持久状态已读 | SYS-015 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/db/models.py` | src；461行 | 是；实现/调用/条件/持久状态已读 | SYS-014 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/db/schema.py` | src；756行 | 是；实现/调用/条件/持久状态已读 | SYS-014 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/db/session.py` | src；21行 | 是；实现/调用/条件/持久状态已读 | SYS-015 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/db/snapshots.py` | src；160行 | 是；实现/调用/条件/持久状态已读 | SYS-015 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/db/uids.py` | src；20行 | 是；实现/调用/条件/持久状态已读 | SYS-015、ACT-012 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/dead_links.py` | src；93行 | 是；实现/调用/条件/持久状态已读 | SYS-019、OPS-008 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/diagnostics.py` | src；127行 | 是；实现/调用/条件/持久状态已读 | UI-015 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/draw_reminder.py` | src；188行 | 是；实现/调用/条件/持久状态已读 | SC-023 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/ds9_contribution_audit.py` | src；194行 | 是；实现/调用/条件/持久状态已读 | UI-008 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/ds9_rejection_audit.py` | src；158行 | 是；实现/调用/条件/持久状态已读 | UI-008 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/favorite_confirmation.py` | src；219行 | 是；实现/调用/条件/持久状态已读 | ACT-006 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/fetch_activity_info.py` | src；404行 | 是；实现/调用/条件/持久状态已读 | OPS-004 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/following_feed.py` | src；1481行 | 是；实现/调用/条件/持久状态已读 | SC-009、SC-010、SC-011 | D-009、D-010 |
| `src/forward_classify_cache.py` | src；63行 | 是；实现/调用/条件/持久状态已读 | SYS-027 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/forward_parse_cache.py` | src；63行 | 是；实现/调用/条件/持久状态已读 | SYS-027 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/forward_parser.py` | src；756行 | 是；实现/调用/条件/持久状态已读 | SYS-027 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/invalid_activities.py` | src；111行 | 是；实现/调用/条件/持久状态已读 | SYS-019 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/job_store.py` | src；286行 | 是；实现/调用/条件/持久状态已读 | JOB-007 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/llm_client.py` | src；378行 | 是；实现/调用/条件/持久状态已读 | SYS-027 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/llm_config.py` | src；66行 | 是；实现/调用/条件/持久状态已读 | UI-014 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/llm_resilience.py` | src；103行 | 是；实现/调用/条件/持久状态已读 | JOB-012、SYS-027 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/llm_settings.py` | src；223行 | 是；实现/调用/条件/持久状态已读 | UI-014 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/log_context.py` | src；56行 | 是；实现/调用/条件/持久状态已读 | SYS-020 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/log_query.py` | src；120行 | 是；实现/调用/条件/持久状态已读 | UI-015 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/log_redact.py` | src；57行 | 是；实现/调用/条件/持久状态已读 | SYS-020 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/log_span.py` | src；172行 | 是；实现/调用/条件/持久状态已读 | SYS-020 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/login_onboarding.py` | src；826行 | 是；实现/调用/条件/持久状态已读 | SYS-003 | D-002 |
| `src/lottery_actions.py` | src；1224行 | 是；实现/调用/条件/持久状态已读 | ACT-004、ACT-005、ACT-006、ACT-007、ACT-008、ACT-011 |D-015、D-021 |
| `src/lottery_api.py` | src；485行 | 是；实现/调用/条件/持久状态已读 | SC-021、ACT-009 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/lottery_classifier.py` | src；242行 | 是；实现/调用/条件/持久状态已读 | SC-021、SYS-019 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/lottery_enricher.py` | src；502行 | 是；实现/调用/条件/持久状态已读 | SC-022 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/lottery_time.py` | src；347行 | 是；实现/调用/条件/持久状态已读 | SC-022、OPS-010 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/manual_ignored_activities.py` | src；118行 | 是；实现/调用/条件/持久状态已读 | UI-002 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/manual_participation_retry.py` | src；200行 | 是；实现/调用/条件/持久状态已读 | ACT-013、ACT-014 | D-012 |
| `src/manual_unknown_retry.py` | src；152行 | 是；实现/调用/条件/持久状态已读 | ACT-013 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/message_api.py` | src；278行 | 是；实现/调用/条件/持久状态已读 | SC-014、NTF-005 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/message_types.py` | src；185行 | 是；实现/调用/条件/持久状态已读 | SC-014、SC-015 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/message_watch.py` | src；100行 | 是；实现/调用/条件/持久状态已读 | NTF-005 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/notification/__init__.py` | src；1行 | 是；包初始化/导出已读 | NTF-001 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/notification/service.py` | src；778行 | 是；实现/调用/条件/持久状态已读 |SC-016、NTF-001、NTF-002、NTF-003、NTF-006 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/observability.py` | src；385行 | 是；实现/调用/条件/持久状态已读 | SYS-020 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/participate_preflight.py` | src；206行 | 是；实现/调用/条件/持久状态已读 | ACT-011 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/participate_text.py` | src；170行 | 是；实现/调用/条件/持久状态已读 | ACT-010 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/participation.py` | src；1334行 | 是；实现/调用/条件/持久状态已读 | ACT-001、ACT-009、ACT-012 | D-015 |
| `src/participation_errors.py` | src；51行 | 是；实现/调用/条件/持久状态已读 | ACT-016 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/participation_guard.py` | src；322行 | 是；实现/调用/条件/持久状态已读 | ACT-007、ACT-012、ACT-015 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/participation_log.py` | src；142行 | 是；实现/调用/条件/持久状态已读 | ACT-016 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/participation_store.py` | src；89行 | 是；实现/调用/条件/持久状态已读 | ACT-016 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/pipeline/__init__.py` | src；3行 | 是；包初始化/导出已读 | SC-018、SC-021 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/pipeline/classify_fetch_context.py` | src；267行 | 是；实现/调用/条件/持久状态已读 | SC-020 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/pipeline/classify_step.py` | src；188行 | 是；实现/调用/条件/持久状态已读 | SC-021 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/pipeline/enrich_fetch_context.py` | src；44行 | 是；实现/调用/条件/持久状态已读 | SC-022 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/pipeline/refresh_all_pipeline.py` | src；1104行 | 是；实现/调用/条件/持久状态已读 | SC-011、SC-018、SC-019 | D-009 |
| `src/pipeline/status_step.py` | src；39行 | 是；实现/调用/条件/持久状态已读 | SC-022 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/platform_risk.py` | src；81行 | 是；实现/调用/条件/持久状态已读 | ACT-018 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/profile_manager.py` | src；298行 | 是；实现/调用/条件/持久状态已读 | SYS-002、SYS-006 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/profile_writer_lease.py` | src；247行 | 是；实现/调用/条件/持久状态已读 | SYS-018 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/repost_cleanup.py` | src；3342行 | 是；实现/调用/条件/持久状态已读 | CLN-001、CLN-002、CLN-003、CLN-004、CLN-005、CLN-006、CLN-007、CLN-008、CLN-009、CLN-010 | D-018 |
| `src/repost_history.py` | src；1057行 | 是；实现/调用/条件/持久状态已读 | CLN-001、CLN-002 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/repost_origin_discovery.py` | src；117行 | 是；实现/调用/条件/持久状态已读 | SC-020 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/restart_control.py` | src；141行 | 是；实现/调用/条件/持久状态已读 | JOB-009、SYS-013 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/retry_utils.py` | src；40行 | 是；实现/调用/条件/持久状态已读 | JOB-011、JOB-012 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/runtime_instances.py` | src；325行 | 是；实现/调用/条件/持久状态已读 | SYS-008 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/runtime_launch.py` | src；121行 | 是；实现/调用/条件/持久状态已读 | SYS-007 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/secrets_inventory.py` | src；110行 | 是；实现/调用/条件/持久状态已读 | SYS-020、UI-015、SYS-023 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/secure_files.py` | src；54行 | 是；实现/调用/条件/持久状态已读 | SYS-017 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/source_outputs.py` | src；18行 | 是；实现/调用/条件/持久状态已读 | SC-018、SYS-015 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/sources/__init__.py` | src；0行 | 是；包初始化/导出已读 | SC-018 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/sources/common.py` | src；278行 | 是；实现/调用/条件/持久状态已读 | SC-001、SC-019 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/sources/ds1_xiaozhuli.py` | src；65行 | 是；实现/调用/条件/持久状态已读 | SC-001 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/sources/ds2_fanqiao.py` | src；28行 | 是；实现/调用/条件/持久状态已读 | SC-002 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/sources/ds3_gongjuren.py` | src；388行 | 是；实现/调用/条件/持久状态已读 | SC-007 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/sources/ds4_junming.py` | src；31行 | 是；实现/调用/条件/持久状态已读 | SC-003 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/sources/ds5_hudong.py` | src；24行 | 是；实现/调用/条件/持久状态已读 | SC-004 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/sources/ds6_nuomi.py` | src；24行 | 是；实现/调用/条件/持久状态已读 | SC-005 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/sources/ds7_dajinli.py` | src；24行 | 是；实现/调用/条件/持久状态已读 | SC-006 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/sources/ds9_topics.py` | src；794行 | 是；实现/调用/条件/持久状态已读 | SC-008、UI-007 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/sources/recent_articles.py` | src；200行 | 是；实现/调用/条件/持久状态已读 | SC-002、SC-003、SC-004、SC-005、SC-006 | D-008 |
| `src/state_seed.py` | src；80行 | 是；实现/调用/条件/持久状态已读 | SYS-016 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/state_store.py` | src；263行 | 是；实现/调用/条件/持久状态已读 | SC-018、SYS-015、SYS-016 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/status_refresh.py` | src；233行 | 是；实现/调用/条件/持久状态已读 | SC-023 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/tray_shutdown.py` | src；75行 | 是；实现/调用/条件/持久状态已读 | SYS-013 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/update_check.py` | src；260行 | 是；实现/调用/条件/持久状态已读 | UI-016 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/user_data.py` | src；44行 | 是；实现/调用/条件/持久状态已读 | SYS-015 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/user_data_io.py` | src；32行 | 是；实现/调用/条件/持久状态已读 | SYS-018 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/user_data_lock.py` | src；9行 | 是；实现/调用/条件/持久状态已读 | SYS-018 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/user_settings.py` | src；111行 | 是；实现/调用/条件/持久状态已读 | ACT-010 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/version_info.py` | src；46行 | 是；实现/调用/条件/持久状态已读 | UI-016、SYS-023 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/watch_feed.py` | src；300行 | 是；实现/调用/条件/持久状态已读 | SC-012、SC-013 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/watch_resume.py` | src；127行 | 是；实现/调用/条件/持久状态已读 | SC-013 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/watch_sync.py` | src；222行 | 是；实现/调用/条件/持久状态已读 | SC-012 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/watch_user_profile.py` | src；62行 | 是；实现/调用/条件/持久状态已读 | UI-006、SC-012 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/watch_users.py` | src；191行 | 是；实现/调用/条件/持久状态已读 | UI-006 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/windows_tray.py` | src；190行 | 是；实现/调用/条件/持久状态已读 | SYS-010、SYS-011、SYS-012 | D-016 |
| `src/winner_scan/__init__.py` | src；1行 | 是；包初始化/导出已读 | SC-014 | 未发现独立疑点；运行行为未在本轮验证 |
| `src/winner_scan/service.py` | src；1663行 | 是；实现/调用/条件/持久状态已读 | SC-014、SC-015、SC-016、NTF-004 | D-003、D-004 |
| `tests/__init__.py` | tests；0行 | 是；fixture/断言/异常注入已读，未执行 | SYS-022 | mock覆盖有限，实际执行待验证 |
| `tests/conftest.py` | tests；137行 | 是；fixture/断言/异常注入已读，未执行 | SYS-022 | mock覆盖有限，实际执行待验证 |
| `tests/offline_guard.py` | tests；215行 | 是；fixture/断言/异常注入已读，未执行 | SYS-022 | mock覆盖有限，实际执行待验证 |
| `tests/scheduler_test_helpers.py` | tests；11行 | 是；fixture/断言/异常注入已读，未执行 | JOB-001、SYS-022 | mock覆盖有限，实际执行待验证 |
| `tests/test_account_service.py` | tests；122行 | 是；fixture/断言/异常注入已读，未执行 | UI-013 | mock覆盖有限，实际执行待验证 |
| `tests/test_active_profile_fail_closed_v18.py` | tests；276行 | 是；fixture/断言/异常注入已读，未执行 | SYS-001、SYS-002、SYS-008、SYS-017、SYS-006 | mock覆盖有限，实际执行待验证 |
| `tests/test_activity_db_performance_v4.py` | tests；252行 | 是；fixture/断言/异常注入已读，未执行 | SYS-014、SYS-015、SYS-016、SC-022、SC-023、ACT-002、UI-001、UI-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_activity_failures_v60.py` | tests；49行 | 是；fixture/断言/异常注入已读，未执行 | UI-003、SYS-019 | mock覆盖有限，实际执行待验证 |
| `tests/test_activity_seed.py` | tests；101行 | 是；fixture/断言/异常注入已读，未执行 | SYS-016、OPS-013 | mock覆盖有限，实际执行待验证 |
| `tests/test_activity_status.py` | tests；57行 | 是；fixture/断言/异常注入已读，未执行 | UI-001、SC-023、ACT-016 | mock覆盖有限，实际执行待验证 |
| `tests/test_activity_summary.py` | tests；97行 | 是；fixture/断言/异常注入已读，未执行 | UI-001、SC-023、SC-022、ACT-002、UI-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_api_errors.py` | tests；176行 | 是；fixture/断言/异常注入已读，未执行 | ACT-018、JOB-012、SYS-021 | mock覆盖有限，实际执行待验证 |
| `tests/test_app_logging.py` | tests；70行 | 是；fixture/断言/异常注入已读，未执行 | SYS-020 | mock覆盖有限，实际执行待验证 |
| `tests/test_app_paths.py` | tests；131行 | 是；fixture/断言/异常注入已读，未执行 | SYS-024 | mock覆盖有限，实际执行待验证 |
| `tests/test_assessment_continuous_batches_m4.py` | tests；435行 | 是；fixture/断言/异常注入已读，未执行 | CLN-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_auto_cleanup_m6.py` | tests；230行 | 是；fixture/断言/异常注入已读，未执行 | CLN-008 | mock覆盖有限，实际执行待验证 |
| `tests/test_auto_remote_state_fail_closed_v17.py` | tests；304行 | 是；fixture/断言/异常注入已读，未执行 | JOB-001、JOB-002、JOB-004、SYS-001、SYS-017、SYS-022、SC-018、ACT-018、JOB-010、ACT-003 | mock覆盖有限，实际执行待验证 |
| `tests/test_auto_scheduler.py` | tests；339行 | 是；fixture/断言/异常注入已读，未执行 | SC-018 | mock覆盖有限，实际执行待验证 |
| `tests/test_auto_scheduler_guard.py` | tests；278行 | 是；fixture/断言/异常注入已读，未执行 | JOB-006 | mock覆盖有限，实际执行待验证 |
| `tests/test_auto_scheduler_lifecycle.py` | tests；367行 | 是；fixture/断言/异常注入已读，未执行 | JOB-001 | mock覆盖有限，实际执行待验证 |
| `tests/test_auto_scheduler_running_state_v102.py` | tests；118行 | 是；fixture/断言/异常注入已读，未执行 | ACT-003 | mock覆盖有限，实际执行待验证 |
| `tests/test_bilibili_login_cookies.py` | tests；35行 | 是；fixture/断言/异常注入已读，未执行 | SYS-026 | mock覆盖有限，实际执行待验证 |
| `tests/test_bilibili_login_stability.py` | tests；121行 | 是；fixture/断言/异常注入已读，未执行 | SYS-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_bilibili_login_status.py` | tests；56行 | 是；fixture/断言/异常注入已读，未执行 | SYS-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_bilibili_rate_limit.py` | tests；202行 | 是；fixture/断言/异常注入已读，未执行 | JOB-011、SYS-026 | mock覆盖有限，实际执行待验证 |
| `tests/test_binggo_launcher.py` | tests；355行 | 是；fixture/断言/异常注入已读，未执行 | SYS-009 | mock覆盖有限，实际执行待验证 |
| `tests/test_bounded_source_history_v89.py` | tests；316行 | 是；fixture/断言/异常注入已读，未执行 | SC-002、SC-003、SC-004、SC-005、SC-006 | mock覆盖有限，实际执行待验证 |
| `tests/test_charging_lottery.py` | tests；37行 | 是；fixture/断言/异常注入已读，未执行 | OPS-009 | mock覆盖有限，实际执行待验证 |
| `tests/test_classify_deleted_link.py` | tests；28行 | 是；fixture/断言/异常注入已读，未执行 | SYS-019、OPS-008 | mock覆盖有限，实际执行待验证 |
| `tests/test_classify_enrich_cache.py` | tests；128行 | 是；fixture/断言/异常注入已读，未执行 | SC-021、SC-022、SC-011、SC-018、SC-019 | mock覆盖有限，实际执行待验证 |
| `tests/test_classify_fetch_context.py` | tests；232行 | 是；fixture/断言/异常注入已读，未执行 | SC-021、SYS-027、SC-020 | mock覆盖有限，实际执行待验证 |
| `tests/test_classify_forward_lottery.py` | tests；63行 | 是；fixture/断言/异常注入已读，未执行 | SC-021、SYS-027 | mock覆盖有限，实际执行待验证 |
| `tests/test_classify_reserve_live.py` | tests；83行 | 是；fixture/断言/异常注入已读，未执行 | ACT-009 | mock覆盖有限，实际执行待验证 |
| `tests/test_cleanup_budget_resume_v58.py` | tests；81行 | 是；fixture/断言/异常注入已读，未执行 | CLN-001、CLN-003、CLN-007、CLN-008、ACT-003、ACT-017、JOB-001、JOB-002、JOB-003、JOB-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_cleanup_scheduler_slim_v59.py` | tests；62行 | 是；fixture/断言/异常注入已读，未执行 | CLN-008、CLN-010 | mock覆盖有限，实际执行待验证 |
| `tests/test_comment_breaker_v50.py` | tests；106行 | 是；fixture/断言/异常注入已读，未执行 | ACT-008、ACT-017、ACT-004、ACT-005、ACT-006、ACT-007、ACT-011、ACT-001、ACT-009、ACT-012 | mock覆盖有限，实际执行待验证 |
| `tests/test_comment_breaker_v51.py` | tests；190行 | 是；fixture/断言/异常注入已读，未执行 | ACT-008、ACT-017 | mock覆盖有限，实际执行待验证 |
| `tests/test_comment_confirmation_v50.py` | tests；102行 | 是；fixture/断言/异常注入已读，未执行 | ACT-008、ACT-017、ACT-018、JOB-011、JOB-012、SYS-026、ACT-004、ACT-005、ACT-006、ACT-007 | mock覆盖有限，实际执行待验证 |
| `tests/test_comment_guard_entrypoints_v81.py` | tests；272行 | 是；fixture/断言/异常注入已读，未执行 | OPS-002 | mock覆盖有限，实际执行待验证 |
| `tests/test_comment_mutation_guard_v81.py` | tests；483行 | 是；fixture/断言/异常注入已读，未执行 | ACT-008、ACT-012 | mock覆盖有限，实际执行待验证 |
| `tests/test_comment_probe_busy_gate_v101.py` | tests；159行 | 是；fixture/断言/异常注入已读，未执行 | ACT-017 | mock覆盖有限，实际执行待验证 |
| `tests/test_comment_probe_v51.py` | tests；670行 | 是；fixture/断言/异常注入已读，未执行 | ACT-017 | mock覆盖有限，实际执行待验证 |
| `tests/test_comment_unknown_diagnostics_v104.py` | tests；271行 | 是；fixture/断言/异常注入已读，未执行 | ACT-008 | mock覆盖有限，实际执行待验证 |
| `tests/test_common.py` | tests；46行 | 是；fixture/断言/异常注入已读，未执行 | SC-017、SC-018、SC-021、SC-001、SC-019 | mock覆盖有限，实际执行待验证 |
| `tests/test_config_health.py` | tests；49行 | 是；fixture/断言/异常注入已读，未执行 | UI-014、SYS-027、UI-016 | mock覆盖有限，实际执行待验证 |
| `tests/test_config_validate.py` | tests；29行 | 是；fixture/断言/异常注入已读，未执行 | UI-014、SYS-027、SYS-026 | mock覆盖有限，实际执行待验证 |
| `tests/test_dashboard_server.py` | tests；77行 | 是；fixture/断言/异常注入已读，未执行 | SYS-009 | mock覆盖有限，实际执行待验证 |
| `tests/test_data_paths.py` | tests；305行 | 是；fixture/断言/异常注入已读，未执行 | SYS-001、SYS-017 | mock覆盖有限，实际执行待验证 |
| `tests/test_dead_links_api.py` | tests；46行 | 是；fixture/断言/异常注入已读，未执行 | OPS-008 | mock覆盖有限，实际执行待验证 |
| `tests/test_delete_repost_lease_v9.py` | tests；54行 | 是；fixture/断言/异常注入已读，未执行 | CLN-008 | mock覆盖有限，实际执行待验证 |
| `tests/test_diagnostics_api.py` | tests；85行 | 是；fixture/断言/异常注入已读，未执行 | UI-015 | mock覆盖有限，实际执行待验证 |
| `tests/test_domestic_notifications_m2.py` | tests；420行 | 是；fixture/断言/异常注入已读，未执行 |NTF-001、NTF-003、NTF-006 | mock覆盖有限，实际执行待验证 |
| `tests/test_draw_reminder.py` | tests；118行 | 是；fixture/断言/异常注入已读，未执行 | SC-023 | mock覆盖有限，实际执行待验证 |
| `tests/test_ds3_article_expansion.py` | tests；543行 | 是；fixture/断言/异常注入已读，未执行 | SC-007 | mock覆盖有限，实际执行待验证 |
| `tests/test_ds3_partial_recovery_v85.py` | tests；297行 | 是；fixture/断言/异常注入已读，未执行 | SC-007 | mock覆盖有限，实际执行待验证 |
| `tests/test_ds3_source_progress.py` | tests；168行 | 是；fixture/断言/异常注入已读，未执行 | SC-007 | mock覆盖有限，实际执行待验证 |
| `tests/test_ds6_integration.py` | tests；38行 | 是；fixture/断言/异常注入已读，未执行 | SC-005、SC-018、SYS-015、ACT-002、UI-001、UI-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_ds9_contribution_audit_v65.py` | tests；76行 | 是；fixture/断言/异常注入已读，未执行 | UI-008 | mock覆盖有限，实际执行待验证 |
| `tests/test_ds9_rejection_audit_v63.py` | tests；113行 | 是；fixture/断言/异常注入已读，未执行 | SC-008、UI-007、UI-008、SC-021、SC-011、SC-018、SC-019 | mock覆盖有限，实际执行待验证 |
| `tests/test_ds9_rejection_audit_v64.py` | tests；56行 | 是；fixture/断言/异常注入已读，未执行 | SC-008、UI-007、UI-008 | mock覆盖有限，实际执行待验证 |
| `tests/test_ds9_topic_display_v106.py` | tests；203行 | 是；fixture/断言/异常注入已读，未执行 | UI-007、UI-008 | mock覆盖有限，实际执行待验证 |
| `tests/test_ds9_topics_m1.py` | tests；713行 | 是；fixture/断言/异常注入已读，未执行 | SC-008 | mock覆盖有限，实际执行待验证 |
| `tests/test_ds_checkpoint_commit.py` | tests；195行 | 是；fixture/断言/异常注入已读，未执行 | SC-001、OPS-001 | mock覆盖有限，实际执行待验证 |
| `tests/test_e2e_hooks.py` | tests；74行 | 是；fixture/断言/异常注入已读，未执行 | SYS-022 | mock覆盖有限，实际执行待验证 |
| `tests/test_enrich_fetch_context.py` | tests；274行 | 是；fixture/断言/异常注入已读，未执行 | SC-022 | mock覆盖有限，实际执行待验证 |
| `tests/test_enrich_risk_no_retry_v12.py` | tests；84行 | 是；fixture/断言/异常注入已读，未执行 | ACT-018、JOB-010、JOB-012、OPS-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_enrich_skip.py` | tests；246行 | 是；fixture/断言/异常注入已读，未执行 | SC-022 | mock覆盖有限，实际执行待验证 |
| `tests/test_error_domain_user_messages_v88.py` | tests；165行 | 是；fixture/断言/异常注入已读，未执行 |JOB-012、SYS-026、UI-017 | mock覆盖有限，实际执行待验证 |
| `tests/test_event_hub.py` | tests；154行 | 是；fixture/断言/异常注入已读，未执行 | JOB-006、JOB-007、JOB-008 | mock覆盖有限，实际执行待验证 |
| `tests/test_expired_activity_filter.py` | tests；282行 | 是；fixture/断言/异常注入已读，未执行 | UI-001、SC-023、SC-022、SC-021、SC-011、SC-018、SC-019、SC-001 | mock覆盖有限，实际执行待验证 |
| `tests/test_favorite_guard_v98.py` | tests；307行 | 是；fixture/断言/异常注入已读，未执行 | ACT-006、ACT-016 | mock覆盖有限，实际执行待验证 |
| `tests/test_favorite_optional.py` | tests；106行 | 是；fixture/断言/异常注入已读，未执行 | ACT-006 | mock覆盖有限，实际执行待验证 |
| `tests/test_fetch_enrich_pending.py` | tests；53行 | 是；fixture/断言/异常注入已读，未执行 | OPS-004、OPS-006 | mock覆盖有限，实际执行待验证 |
| `tests/test_fetch_path_effectiveness.py` | tests；182行 | 是；fixture/断言/异常注入已读，未执行 | OPS-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_following_batch_persistence_v115.py` | tests；346行 | 是；fixture/断言/异常注入已读，未执行 | SC-009、SC-011 | mock覆盖有限，实际执行待验证 |
| `tests/test_following_feed.py` | tests；254行 | 是；fixture/断言/异常注入已读，未执行 | SC-009 | mock覆盖有限，实际执行待验证 |
| `tests/test_following_feed_checkpoint_v16.py` | tests；409行 | 是；fixture/断言/异常注入已读，未执行 | SC-010 | mock覆盖有限，实际执行待验证 |
| `tests/test_following_feed_incremental_v69.py` | tests；173行 | 是；fixture/断言/异常注入已读，未执行 | SC-009、SC-010、SC-011 | mock覆盖有限，实际执行待验证 |
| `tests/test_following_feed_v74.py` | tests；474行 | 是；fixture/断言/异常注入已读，未执行 | SC-009、SC-010、SC-011、SYS-001、SYS-017、SC-014、NTF-005、ACT-018、JOB-001、SYS-022 | mock覆盖有限，实际执行待验证 |
| `tests/test_following_feed_v97.py` | tests；284行 | 是；fixture/断言/异常注入已读，未执行 | SC-009、SC-010 | mock覆盖有限，实际执行待验证 |
| `tests/test_formal_risk_propagation_v82.py` | tests；312行 | 是；fixture/断言/异常注入已读，未执行 | ACT-018 | mock覆盖有限，实际执行待验证 |
| `tests/test_forward_lottery_time_resolve.py` | tests；32行 | 是；fixture/断言/异常注入已读，未执行 | SC-022、SYS-027、OPS-010 | mock覆盖有限，实际执行待验证 |
| `tests/test_forward_parser_extract.py` | tests；171行 | 是；fixture/断言/异常注入已读，未执行 | SYS-027 | mock覆盖有限，实际执行待验证 |
| `tests/test_forward_parser_prize.py` | tests；106行 | 是；fixture/断言/异常注入已读，未执行 | SC-022、SYS-027 | mock覆盖有限，实际执行待验证 |
| `tests/test_frontend_cleanup_v5.py` | tests；58行 | 是；fixture/断言/异常注入已读，未执行 | CLN-001、CLN-003、CLN-007、CLN-008 | mock覆盖有限，实际执行待验证 |
| `tests/test_frontend_static.py` | tests；62行 | 是；fixture/断言/异常注入已读，未执行 | UI-001、UI-011、SYS-021 | mock覆盖有限，实际执行待验证 |
| `tests/test_git_secrets_not_tracked.py` | tests；28行 | 是；fixture/断言/异常注入已读，未执行 | SYS-020、SYS-021、UI-015、SYS-023 | mock覆盖有限，实际执行待验证 |
| `tests/test_invalid_4101152_v52.py` | tests；157行 | 是；fixture/断言/异常注入已读，未执行 | SC-022、SYS-019、SC-021、ACT-009、ACT-011、ACT-001、ACT-012、ACT-016 | mock覆盖有限，实际执行待验证 |
| `tests/test_invalid_activity_log_v40.py` | tests；164行 | 是；fixture/断言/异常注入已读，未执行 | UI-001、SC-023、SC-022、SYS-019、SYS-020、ACT-004、ACT-005、ACT-006、ACT-007、ACT-008 | mock覆盖有限，实际执行待验证 |
| `tests/test_job_events_publish.py` | tests；69行 | 是；fixture/断言/异常注入已读，未执行 | JOB-006、JOB-007、JOB-008、ACT-003 | mock覆盖有限，实际执行待验证 |
| `tests/test_job_runner_login.py` | tests；229行 | 是；fixture/断言/异常注入已读，未执行 | JOB-006、OPS-020 | mock覆盖有限，实际执行待验证 |
| `tests/test_job_store.py` | tests；157行 | 是；fixture/断言/异常注入已读，未执行 | JOB-006、JOB-007 | mock覆盖有限，实际执行待验证 |
| `tests/test_llm_client.py` | tests；45行 | 是；fixture/断言/异常注入已读，未执行 | SYS-027 | mock覆盖有限，实际执行待验证 |
| `tests/test_llm_resilience_v21.py` | tests；635行 | 是；fixture/断言/异常注入已读，未执行 | JOB-012、SYS-027 | mock覆盖有限，实际执行待验证 |
| `tests/test_llm_settings.py` | tests；25行 | 是；fixture/断言/异常注入已读，未执行 | UI-014、OPS-022 | mock覆盖有限，实际执行待验证 |
| `tests/test_llm_timeout_v11.py` | tests；287行 | 是；fixture/断言/异常注入已读，未执行 | UI-014、SYS-027、ACT-018、JOB-010 | mock覆盖有限，实际执行待验证 |
| `tests/test_local_control_plane_security_v8.py` | tests；267行 | 是；fixture/断言/异常注入已读，未执行 | SYS-021 | mock覆盖有限，实际执行待验证 |
| `tests/test_local_read_performance_v3.py` | tests；495行 | 是；fixture/断言/异常注入已读，未执行 | UI-013、NTF-005、SYS-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_log_context.py` | tests；25行 | 是；fixture/断言/异常注入已读，未执行 | SYS-020 | mock覆盖有限，实际执行待验证 |
| `tests/test_log_jsonl_formatter.py` | tests；75行 | 是；fixture/断言/异常注入已读，未执行 | SYS-020 | mock覆盖有限，实际执行待验证 |
| `tests/test_log_query.py` | tests；54行 | 是；fixture/断言/异常注入已读，未执行 | UI-015 | mock覆盖有限，实际执行待验证 |
| `tests/test_log_redact.py` | tests；32行 | 是；fixture/断言/异常注入已读，未执行 | SYS-020 | mock覆盖有限，实际执行待验证 |
| `tests/test_log_retention_frequency_v92.py` | tests；275行 | 是；fixture/断言/异常注入已读，未执行 | SYS-020 | mock覆盖有限，实际执行待验证 |
| `tests/test_login_identity_guard_v8.py` | tests；188行 | 是；fixture/断言/异常注入已读，未执行 | ACT-005、ACT-011、SYS-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_login_onboarding_m7.py` | tests；419行 | 是；fixture/断言/异常注入已读，未执行 | SYS-003 | mock覆盖有限，实际执行待验证 |
| `tests/test_lottery_api_opus.py` | tests；95行 | 是；fixture/断言/异常注入已读，未执行 | SC-022、SYS-027、SC-021、ACT-009 | mock覆盖有限，实际执行待验证 |
| `tests/test_lottery_classifier_upower.py` | tests；13行 | 是；fixture/断言/异常注入已读，未执行 | SC-021、SYS-019、OPS-009 | mock覆盖有限，实际执行待验证 |
| `tests/test_lottery_date_priority_v55.py` | tests；173行 | 是；fixture/断言/异常注入已读，未执行 | SC-022、SYS-027、OPS-010、ACT-002、UI-001、UI-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_lottery_time.py` | tests；91行 | 是；fixture/断言/异常注入已读，未执行 | SC-022 | mock覆盖有限，实际执行待验证 |
| `tests/test_lottery_time_default.py` | tests；28行 | 是；fixture/断言/异常注入已读，未执行 | OPS-010 | mock覆盖有限，实际执行待验证 |
| `tests/test_lottery_time_infer.py` | tests；9行 | 是；fixture/断言/异常注入已读，未执行 | OPS-010 | mock覆盖有限，实际执行待验证 |
| `tests/test_manual_cleanup_run_to_completion_v77.py` | tests；361行 | 是；fixture/断言/异常注入已读，未执行 | JOB-011、CLN-002、CLN-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_manual_ignore_v76.py` | tests；108行 | 是；fixture/断言/异常注入已读，未执行 | UI-002 | mock覆盖有限，实际执行待验证 |
| `tests/test_mcp_job_identity.py` | tests；145行 | 是；fixture/断言/异常注入已读，未执行 | OPS-019、OPS-020、OPS-021、OPS-022、OPS-023 | mock覆盖有限，实际执行待验证 |
| `tests/test_merge_type_mismatch.py` | tests；32行 | 是；fixture/断言/异常注入已读，未执行 | SC-022、SYS-027、OPS-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_message_api.py` | tests；358行 | 是；fixture/断言/异常注入已读，未执行 | NTF-005、OPS-003 | mock覆盖有限，实际执行待验证 |
| `tests/test_message_watch.py` | tests；60行 | 是；fixture/断言/异常注入已读，未执行 | NTF-005 | mock覆盖有限，实际执行待验证 |
| `tests/test_migration_function_clarity_v93.py` | tests；144行 | 是；fixture/断言/异常注入已读，未执行 | SYS-014、SYS-015、SYS-016 | mock覆盖有限，实际执行待验证 |
| `tests/test_missing_enrich.py` | tests；45行 | 是；fixture/断言/异常注入已读，未执行 | OPS-004、OPS-006 | mock覆盖有限，实际执行待验证 |
| `tests/test_multi_profile_scan_stagger_scope_v116.py` | tests；132行 | 是；fixture/断言/异常注入已读，未执行 | JOB-003 | mock覆盖有限，实际执行待验证 |
| `tests/test_multi_profile_scan_stagger_v111.py` | tests；235行 | 是；fixture/断言/异常注入已读，未执行 | JOB-003 | mock覆盖有限，实际执行待验证 |
| `tests/test_night_quiet_scheduler_v99.py` | tests；211行 | 是；fixture/断言/异常注入已读，未执行 | JOB-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_notification_outbox_recovery_v11.py` | tests；123行 | 是；fixture/断言/异常注入已读，未执行 | NTF-002 | mock覆盖有限，实际执行待验证 |
| `tests/test_observability_consistency_v15.py` | tests；252行 | 是；fixture/断言/异常注入已读，未执行 | SYS-020 | mock覆盖有限，实际执行待验证 |
| `tests/test_observability_malformed_incomplete_v16.py` | tests；155行 | 是；fixture/断言/异常注入已读，未执行 | SYS-020、SC-014、SC-015、SC-008、UI-007、SC-016、NTF-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_observability_v75.py` | tests；389行 | 是；fixture/断言/异常注入已读，未执行 | SYS-020、ACT-018、JOB-011、JOB-012、SYS-026、SYS-027、JOB-007、ACT-003、JOB-006 | mock覆盖有限，实际执行待验证 |
| `tests/test_offline_isolation_v6.py` | tests；203行 | 是；fixture/断言/异常注入已读，未执行 | SYS-022 | mock覆盖有限，实际执行待验证 |
| `tests/test_onboarding_complete_contract_v30.py` | tests；68行 | 是；fixture/断言/异常注入已读，未执行 | SYS-003 | mock覆盖有限，实际执行待验证 |
| `tests/test_onboarding_complete_e2e_v30.py` | tests；265行 | 是；fixture/断言/异常注入已读，未执行 | SYS-003、SYS-004、SYS-001、SYS-017、SYS-002、SYS-006、JOB-009、SYS-013 | mock覆盖有限，实际执行待验证 |
| `tests/test_onboarding_poll_v28.py` | tests；500行 | 是；fixture/断言/异常注入已读，未执行 | SYS-003、SYS-004、SYS-001、SYS-017、SYS-002、SYS-006 | mock覆盖有限，实际执行待验证 |
| `tests/test_onboarding_secret_staging_v31.py` | tests；432行 | 是；fixture/断言/异常注入已读，未执行 | SYS-003、SYS-004、SYS-001、SYS-017、SYS-002、SYS-006 | mock覆盖有限，实际执行待验证 |
| `tests/test_onboarding_sweep_v9.py` | tests；226行 | 是；fixture/断言/异常注入已读，未执行 | SYS-003、SYS-001、SYS-017 | mock覆盖有限，实际执行待验证 |
| `tests/test_operational_polish_v10.py` | tests；219行 | 是；fixture/断言/异常注入已读，未执行 | SYS-001、SYS-018、SYS-022、OPS-010、OPS-011、OPS-012、OPS-001、SYS-017、SYS-002、SYS-006 | mock覆盖有限，实际执行待验证 |
| `tests/test_opus_detail_strict.py` | tests；174行 | 是；fixture/断言/异常注入已读，未执行 | ACT-018、JOB-011、JOB-012、SYS-026、SC-021、ACT-009 | mock覆盖有限，实际执行待验证 |
| `tests/test_participate_preflight.py` | tests；133行 | 是；fixture/断言/异常注入已读，未执行 | ACT-001、ACT-002、ACT-012、ACT-016、SC-022、ACT-011、ACT-009 | mock覆盖有限，实际执行待验证 |
| `tests/test_participate_reserve.py` | tests；249行 | 是；fixture/断言/异常注入已读，未执行 | ACT-001、ACT-002、ACT-012、ACT-016、ACT-004、ACT-005、ACT-006、ACT-007、ACT-008、ACT-011 | mock覆盖有限，实际执行待验证 |
| `tests/test_participate_text.py` | tests；192行 | 是；fixture/断言/异常注入已读，未执行 | ACT-010 | mock覆盖有限，实际执行待验证 |
| `tests/test_participate_triple.py` | tests；560行 | 是；fixture/断言/异常注入已读，未执行 | ACT-001、ACT-002、ACT-012、ACT-016、UI-001、UI-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_participation_dedup_entrypoints.py` | tests；284行 | 是；fixture/断言/异常注入已读，未执行 | OPS-002 | mock覆盖有限，实际执行待验证 |
| `tests/test_participation_dedup_flow.py` | tests；357行 | 是；fixture/断言/异常注入已读，未执行 | ACT-016 | mock覆盖有限，实际执行待验证 |
| `tests/test_participation_guard.py` | tests；235行 | 是；fixture/断言/异常注入已读，未执行 | ACT-007、ACT-012 | mock覆盖有限，实际执行待验证 |
| `tests/test_participation_guard_schema.py` | tests；253行 | 是；fixture/断言/异常注入已读，未执行 | SYS-014 | mock覆盖有限，实际执行待验证 |
| `tests/test_participation_guards.py` | tests；21行 | 是；fixture/断言/异常注入已读，未执行 | ACT-002、ACT-011 | mock覆盖有限，实际执行待验证 |
| `tests/test_participation_interactions.py` | tests；348行 | 是；fixture/断言/异常注入已读，未执行 | ACT-004、ACT-005 | mock覆盖有限，实际执行待验证 |
| `tests/test_participation_risk_breaker_v15.py` | tests；518行 | 是；fixture/断言/异常注入已读，未执行 | ACT-001、ACT-002、ACT-012、ACT-016、ACT-018、JOB-011、JOB-012、SYS-026、ACT-004、ACT-005 | mock覆盖有限，实际执行待验证 |
| `tests/test_participation_success.py` | tests；81行 | 是；fixture/断言/异常注入已读，未执行 | ACT-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_participation_validation.py` | tests；19行 | 是；fixture/断言/异常注入已读，未执行 | ACT-001、ACT-002、ACT-012、ACT-016、ACT-009、SC-001、SC-019 | mock覆盖有限，实际执行待验证 |
| `tests/test_pipeline_db_reads_v91.py` | tests；240行 | 是；fixture/断言/异常注入已读，未执行 | SC-019 | mock覆盖有限，实际执行待验证 |
| `tests/test_pipeline_persisted_state.py` | tests；54行 | 是；fixture/断言/异常注入已读，未执行 | SC-022、SYS-015 | mock覆盖有限，实际执行待验证 |
| `tests/test_profile_api.py` | tests；333行 | 是；fixture/断言/异常注入已读，未执行 | JOB-009、SYS-002、SYS-005 | mock覆盖有限，实际执行待验证 |
| `tests/test_profile_lock_v9.py` | tests；158行 | 是；fixture/断言/异常注入已读，未执行 | SYS-008 | mock覆盖有限，实际执行待验证 |
| `tests/test_profile_manager.py` | tests；151行 | 是；fixture/断言/异常注入已读，未执行 | SYS-002、SYS-006 | mock覆盖有限，实际执行待验证 |
| `tests/test_profile_state_atomicity_v10.py` | tests；394行 | 是；fixture/断言/异常注入已读，未执行 | SYS-018 | mock覆盖有限，实际执行待验证 |
| `tests/test_profile_writer_lease_m5.py` | tests；366行 | 是；fixture/断言/异常注入已读，未执行 | SYS-018、OPS-001、OPS-005、OPS-007、OPS-012、OPS-014 | mock覆盖有限，实际执行待验证 |
| `tests/test_realtime_consistency_v2.py` | tests；190行 | 是；fixture/断言/异常注入已读，未执行 | JOB-007 | mock覆盖有限，实际执行待验证 |
| `tests/test_refresh_all_bad_dynamic.py` | tests；167行 | 是；fixture/断言/异常注入已读，未执行 | SC-021 | mock覆盖有限，实际执行待验证 |
| `tests/test_refresh_all_enrich_progress.py` | tests；61行 | 是；fixture/断言/异常注入已读，未执行 | JOB-007 | mock覆盖有限，实际执行待验证 |
| `tests/test_refresh_all_progress.py` | tests；27行 | 是；fixture/断言/异常注入已读，未执行 | SC-017、SC-018、SC-021 | mock覆盖有限，实际执行待验证 |
| `tests/test_refresh_source.py` | tests；42行 | 是；fixture/断言/异常注入已读，未执行 | SC-017、SC-018、SC-021 | mock覆盖有限，实际执行待验证 |
| `tests/test_repost_cleanup_api.py` | tests；414行 | 是；fixture/断言/异常注入已读，未执行 | CLN-005 | mock覆盖有限，实际执行待验证 |
| `tests/test_repost_cleanup_candidates.py` | tests；158行 | 是；fixture/断言/异常注入已读，未执行 | CLN-003、CLN-007 | mock覆盖有限，实际执行待验证 |
| `tests/test_repost_cleanup_service.py` | tests；313行 | 是；fixture/断言/异常注入已读，未执行 | CLN-001、CLN-003、CLN-007 | mock覆盖有限，实际执行待验证 |
| `tests/test_repost_cleanup_v2_assessment.py` | tests；228行 | 是；fixture/断言/异常注入已读，未执行 | CLN-001、CLN-003、CLN-007、CLN-008、SC-021、CLN-002、CLN-004、CLN-005、CLN-006、CLN-009 | mock覆盖有限，实际执行待验证 |
| `tests/test_repost_cleanup_v3_smart.py` | tests；194行 | 是；fixture/断言/异常注入已读，未执行 | CLN-006 | mock覆盖有限，实际执行待验证 |
| `tests/test_repost_cleanup_v3_wrapup.py` | tests；261行 | 是；fixture/断言/异常注入已读，未执行 | CLN-006 | mock覆盖有限，实际执行待验证 |
| `tests/test_repost_cleanup_v5_maintain.py` | tests；507行 | 是；fixture/断言/异常注入已读，未执行 | CLN-001、CLN-003、CLN-007、CLN-008、SYS-001、SYS-017、SYS-014、ACT-007、ACT-012、ACT-015 | mock覆盖有限，实际执行待验证 |
| `tests/test_repost_cleanup_v6_health.py` | tests；303行 | 是；fixture/断言/异常注入已读，未执行 | CLN-001、CLN-003、CLN-007、CLN-008、SYS-014、CLN-002、CLN-004、CLN-005、CLN-006、CLN-009 | mock覆盖有限，实际执行待验证 |
| `tests/test_repost_cleanup_v6_longrun.py` | tests；226行 | 是；fixture/断言/异常注入已读，未执行 | CLN-001、CLN-003、CLN-007、CLN-008、SYS-014、ACT-007、ACT-012、ACT-015、CLN-002、CLN-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_repost_cleanup_v79.py` | tests；137行 | 是；fixture/断言/异常注入已读，未执行 | CLN-003 | mock覆盖有限，实际执行待验证 |
| `tests/test_repost_cleanup_v80.py` | tests；104行 | 是；fixture/断言/异常注入已读，未执行 | CLN-003 | mock覆盖有限，实际执行待验证 |
| `tests/test_repost_delete_early_stop_v87.py` | tests；254行 | 是；fixture/断言/异常注入已读，未执行 | CLN-007、CLN-009 | mock覆盖有限，实际执行待验证 |
| `tests/test_repost_history.py` | tests；288行 | 是；fixture/断言/异常注入已读，未执行 | CLN-001 | mock覆盖有限，实际执行待验证 |
| `tests/test_repost_history_schema.py` | tests；423行 | 是；fixture/断言/异常注入已读，未执行 | SYS-014 | mock覆盖有限，实际执行待验证 |
| `tests/test_repost_origin_secondary_v107.py` | tests；304行 | 是；fixture/断言/异常注入已读，未执行 | SC-020 | mock覆盖有限，实际执行待验证 |
| `tests/test_request_budget_v22.py` | tests；842行 | 是；fixture/断言/异常注入已读，未执行 | JOB-011 | mock覆盖有限，实际执行待验证 |
| `tests/test_reserve_no_retry_v9.py` | tests；109行 | 是；fixture/断言/异常注入已读，未执行 | ACT-009 | mock覆盖有限，实际执行待验证 |
| `tests/test_restart_control.py` | tests；116行 | 是；fixture/断言/异常注入已读，未执行 | JOB-009、SYS-005 | mock覆盖有限，实际执行待验证 |
| `tests/test_risk_cooldown_v20.py` | tests；163行 | 是；fixture/断言/异常注入已读，未执行 | JOB-010 | mock覆盖有限，实际执行待验证 |
| `tests/test_risk_propagation_v14.py` | tests；104行 | 是；fixture/断言/异常注入已读，未执行 | ACT-018、JOB-010、JOB-012、UI-006、SC-012 | mock覆盖有限，实际执行待验证 |
| `tests/test_risk_semantics_v13.py` | tests；103行 | 是；fixture/断言/异常注入已读，未执行 | ACT-018 | mock覆盖有限，实际执行待验证 |
| `tests/test_risk_semantics_v2.py` | tests；635行 | 是；fixture/断言/异常注入已读，未执行 | ACT-018、JOB-010、JOB-012、SC-022、SC-009、SC-010、SC-011、SC-018、SC-019、CLN-001 | mock覆盖有限，实际执行待验证 |
| `tests/test_run_e2e_server.py` | tests；30行 | 是；fixture/断言/异常注入已读，未执行 | SYS-022 | mock覆盖有限，实际执行待验证 |
| `tests/test_runtime_api.py` | tests；24行 | 是；fixture/断言/异常注入已读，未执行 | UI-016、SYS-021 | mock覆盖有限，实际执行待验证 |
| `tests/test_runtime_instances_v109.py` | tests；497行 | 是；fixture/断言/异常注入已读，未执行 | SYS-001、SYS-006、SYS-007、SYS-008 | mock覆盖有限，实际执行待验证 |
| `tests/test_runtime_path_performance_v7.py` | tests；238行 | 是；fixture/断言/异常注入已读，未执行 | SYS-001、SYS-018、SYS-022、SYS-017、SYS-014 | mock覆盖有限，实际执行待验证 |
| `tests/test_scan_observability.py` | tests；192行 | 是；fixture/断言/异常注入已读，未执行 | SYS-020 | mock覆盖有限，实际执行待验证 |
| `tests/test_scheduler_cadence_v26.py` | tests；352行 | 是；fixture/断言/异常注入已读，未执行 | JOB-002 | mock覆盖有限，实际执行待验证 |
| `tests/test_scheduler_day_night_v72.py` | tests；120行 | 是；fixture/断言/异常注入已读，未执行 | JOB-002、JOB-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_scheduler_jitter_v38.py` | tests；540行 | 是；fixture/断言/异常注入已读，未执行 | ACT-003、ACT-016 | mock覆盖有限，实际执行待验证 |
| `tests/test_scheduler_orchestration_m8.py` | tests；371行 | 是；fixture/断言/异常注入已读，未执行 | JOB-001、CLN-010 | mock覆盖有限，实际执行待验证 |
| `tests/test_scheduler_resilience_v24.py` | tests；368行 | 是；fixture/断言/异常注入已读，未执行 | SC-018、JOB-010 | mock覆盖有限，实际执行待验证 |
| `tests/test_scheduler_slot_atomic_v68.py` | tests；168行 | 是；fixture/断言/异常注入已读，未执行 | JOB-005 | mock覆盖有限，实际执行待验证 |
| `tests/test_scheduler_slot_persistence_v9.py` | tests；225行 | 是；fixture/断言/异常注入已读，未执行 | JOB-005 | mock覆盖有限，实际执行待验证 |
| `tests/test_secrets_inventory.py` | tests；51行 | 是；fixture/断言/异常注入已读，未执行 | OPS-015 | mock覆盖有限，实际执行待验证 |
| `tests/test_secure_files.py` | tests；30行 | 是；fixture/断言/异常注入已读，未执行 | SYS-017 | mock覆盖有限，实际执行待验证 |
| `tests/test_sources_common.py` | tests；68行 | 是；fixture/断言/异常注入已读，未执行 | SC-001 | mock覆盖有限，实际执行待验证 |
| `tests/test_sqlite_data_layer.py` | tests；187行 | 是；fixture/断言/异常注入已读，未执行 | SC-022、SYS-014、SYS-015、SYS-016、OPS-014、OPS-016 | mock覆盖有限，实际执行待验证 |
| `tests/test_sse_events.py` | tests；75行 | 是；fixture/断言/异常注入已读，未执行 | JOB-008 | mock覆盖有限，实际执行待验证 |
| `tests/test_state_seed.py` | tests；72行 | 是；fixture/断言/异常注入已读，未执行 | SYS-016、OPS-013 | mock覆盖有限，实际执行待验证 |
| `tests/test_status_refresh.py` | tests；199行 | 是；fixture/断言/异常注入已读，未执行 | SC-023 | mock覆盖有限，实际执行待验证 |
| `tests/test_system_tray_v110.py` | tests；377行 | 是；fixture/断言/异常注入已读，未执行 | SYS-010、SYS-013、SYS-023、OPS-018 | mock覆盖有限，实际执行待验证 |
| `tests/test_tray_health_tooltip_v113.py` | tests；203行 | 是；fixture/断言/异常注入已读，未执行 | SYS-012 | mock覆盖有限，实际执行待验证 |
| `tests/test_tray_runtime_lifecycle_v112a.py` | tests；198行 | 是；fixture/断言/异常注入已读，未执行 | SYS-009、SYS-011、SYS-013 | mock覆盖有限，实际执行待验证 |
| `tests/test_unknown_manual_retry_v112.py` | tests；579行 | 是；fixture/断言/异常注入已读，未执行 | ACT-001、ACT-013、ACT-014 | mock覆盖有限，实际执行待验证 |
| `tests/test_update_all_datasources.py` | tests；457行 | 是；fixture/断言/异常注入已读，未执行 | SC-017 | mock覆盖有限，实际执行待验证 |
| `tests/test_update_check.py` | tests；127行 | 是；fixture/断言/异常注入已读，未执行 | UI-016 | mock覆盖有限，实际执行待验证 |
| `tests/test_updates_api.py` | tests；60行 | 是；fixture/断言/异常注入已读，未执行 | UI-016 | mock覆盖有限，实际执行待验证 |
| `tests/test_user_data_io.py` | tests；28行 | 是；fixture/断言/异常注入已读，未执行 | SYS-018 | mock覆盖有限，实际执行待验证 |
| `tests/test_user_messages.py` | tests；79行 | 是；fixture/断言/异常注入已读，未执行 | ACT-018、JOB-012 | mock覆盖有限，实际执行待验证 |
| `tests/test_v33_canonical_identity.py` | tests；405行 | 是；fixture/断言/异常注入已读，未执行 | SC-019、SC-020 | mock覆盖有限，实际执行待验证 |
| `tests/test_v33_following_snapshot.py` | tests；264行 | 是；fixture/断言/异常注入已读，未执行 | SC-009、SC-010、SC-011、JOB-011 | mock覆盖有限，实际执行待验证 |
| `tests/test_v36_remove_confirmation_ui.py` | tests；121行 | 是；fixture/断言/异常注入已读，未执行 | ACT-002 | mock覆盖有限，实际执行待验证 |
| `tests/test_v5_history_guard_reconciliation.py` | tests；374行 | 是；fixture/断言/异常注入已读，未执行 | ACT-015、CLN-001 | mock覆盖有限，实际执行待验证 |
| `tests/test_v5_participation_isolation.py` | tests；143行 | 是；fixture/断言/异常注入已读，未执行 | ACT-007 | mock覆盖有限，实际执行待验证 |
| `tests/test_v5_reconciliation.py` | tests；281行 | 是；fixture/断言/异常注入已读，未执行 | CLN-002 | mock覆盖有限，实际执行待验证 |
| `tests/test_version_info.py` | tests；34行 | 是；fixture/断言/异常注入已读，未执行 | UI-016、SYS-001、SYS-023 | mock覆盖有限，实际执行待验证 |
| `tests/test_watch_truncated_resume_v86.py` | tests；552行 | 是；fixture/断言/异常注入已读，未执行 | SC-012、SC-013 | mock覆盖有限，实际执行待验证 |
| `tests/test_watch_user_profile.py` | tests；35行 | 是；fixture/断言/异常注入已读，未执行 | SC-012、SC-013、UI-006 | mock覆盖有限，实际执行待验证 |
| `tests/test_watch_users.py` | tests；116行 | 是；fixture/断言/异常注入已读，未执行 | SC-012、OPS-011 | mock覆盖有限，实际执行待验证 |
| `tests/test_watch_users_storage_v12.py` | tests；218行 | 是；fixture/断言/异常注入已读，未执行 | UI-006 | mock覆盖有限，实际执行待验证 |
| `tests/test_web_api.py` | tests；171行 | 是；fixture/断言/异常注入已读，未执行 | UI-001、SYS-021 | mock覆盖有限，实际执行待验证 |
| `tests/test_winner_auto_reply_v25.py` | tests；603行 | 是；fixture/断言/异常注入已读，未执行 | SC-014、SC-015、SC-016、NTF-004、NTF-001、NTF-002、NTF-003 | mock覆盖有限，实际执行待验证 |
| `tests/test_winner_auto_reply_v53.py` | tests；195行 | 是；fixture/断言/异常注入已读，未执行 | SC-015 | mock覆盖有限，实际执行待验证 |
| `tests/test_winner_cursor_v83.py` | tests；367行 | 是；fixture/断言/异常注入已读，未执行 | SC-014 | mock覆盖有限，实际执行待验证 |
| `tests/test_winner_deep_scan_m3.py` | tests；1112行 | 是；fixture/断言/异常注入已读，未执行 | SC-014、SC-016、NTF-004 | mock覆盖有限，实际执行待验证 |
| `tests/test_winner_incremental_v70.py` | tests；360行 | 是；fixture/断言/异常注入已读，未执行 | SC-015 | mock覆盖有限，实际执行待验证 |
| `tests/test_winner_notification_copy_v54.py` | tests；108行 | 是；fixture/断言/异常注入已读，未执行 | NTF-002 | mock覆盖有限，实际执行待验证 |
| `tests/test_winner_notification_recovery_v84.py` | tests；335行 | 是；fixture/断言/异常注入已读，未执行 | SC-016 | mock覆盖有限，实际执行待验证 |
| `tests/test_winner_stable_key_v12.py` | tests；98行 | 是；fixture/断言/异常注入已读，未执行 | SC-014、SC-015、SC-016、NTF-004 | mock覆盖有限，实际执行待验证 |
| `web/__init__.py` | web；0行 | 是；包初始化/导出已读 | SYS-021 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/account_service.py` | web；472行 | 是；实现/调用/条件/持久状态已读 | UI-013、NTF-005、SYS-004 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/actions.py` | web；2385行 | 是；实现/调用/条件/持久状态已读 | SC-001、SC-008、SC-012、SC-017、SC-018、ACT-002、ACT-003、ACT-014、JOB-006、CLN-004、CLN-009 | D-012、D-017 |
| `web/activity_service.py` | web；715行 | 是；实现/调用/条件/持久状态已读 | ACT-002、UI-001、UI-004 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/api_contract.py` | web；91行 | 是；实现/调用/条件/持久状态已读 | SYS-021 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/api_errors.py` | web；233行 | 是；实现/调用/条件/持久状态已读 | JOB-012 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/app.py` | web；1642行 | 是；实现/调用/条件/持久状态已读 | ACT-001、UI-002、UI-006、CLN-007、CLN-010、NTF-003、SYS-002、SYS-003、SYS-005、SYS-006、SYS-007、SYS-021 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/auto_config.py` | web；109行 | 是；实现/调用/条件/持久状态已读 | SC-018、JOB-001、JOB-002、JOB-004 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/auto_remote_state.py` | web；224行 | 是；实现/调用/条件/持久状态已读 | ACT-018、JOB-010 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/auto_scheduler.py` | web；1620行 | 是；实现/调用/条件/持久状态已读 | ACT-003、ACT-017、JOB-001、JOB-002、JOB-003、JOB-004、JOB-005、JOB-010、UI-010 | D-011 |
| `web/e2e_hooks.py` | web；186行 | 是；实现/调用/条件/持久状态已读 | SYS-022 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/e2e_seed.py` | web；130行 | 是；实现/调用/条件/持久状态已读 | SYS-022 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/enrich_prune.py` | web；43行 | 是；实现/调用/条件/持久状态已读 | SC-023、UI-002 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/event_hub.py` | web；216行 | 是；实现/调用/条件/持久状态已读 | JOB-008 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/e2e/helpers.ts` | web；38行 | 是；fixture/断言/异常注入已读，未执行 | SYS-022 | mock覆盖有限，实际执行待验证 |
| `web/frontend/e2e/smoke.spec.ts` | web；129行 | 是；fixture/断言/异常注入已读，未执行 | SYS-022 | D-013 |
| `web/frontend/index.html` | web；718行 | 是；布局/交互状态及使用关系已读 | UI-011、UI-012、UI-009 | D-013 |
| `web/frontend/playwright.config.ts` | web；40行 | 是；实现/调用/条件/持久状态已读 | SYS-022 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/account/account-preserve.test.ts` | web；40行 | 是；fixture/断言/异常注入已读，未执行 | UI-013 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/account/account-preserve.ts` | web；21行 | 是；实现/调用/条件/持久状态已读 | UI-013 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/account/index.ts` | web；624行 | 是；实现/调用/条件/持久状态已读 | UI-013 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/account/login-state.test.ts` | web；188行 | 是；fixture/断言/异常注入已读，未执行 | UI-013 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/activities/activity-loading.test.ts` | web；167行 | 是；fixture/断言/异常注入已读，未执行 | UI-001 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/activities/delete-feedback-v103.test.ts` | web；206行 | 是；fixture/断言/异常注入已读，未执行 | UI-003 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/activities/index.ts` | web；579行 | 是；实现/调用/条件/持久状态已读 | UI-001、UI-002、UI-003 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/activities/participation-guard.test.ts` | web；53行 | 是；fixture/断言/异常注入已读，未执行 | UI-002 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/api/client.test.ts` | web；34行 | 是；fixture/断言/异常注入已读，未执行 |JOB-012、SYS-021、UI-017 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/api/client.ts` | web；73行 | 是；实现/调用/条件/持久状态已读 | JOB-012、SYS-021 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/auto/idle-countdown.test.ts` | web；171行 | 是；fixture/断言/异常注入已读，未执行 | UI-010 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/auto/index.ts` | web；579行 | 是；实现/调用/条件/持久状态已读 | UI-010 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/auto/op-race.test.ts` | web；335行 | 是；fixture/断言/异常注入已读，未执行 | UI-010 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/bootstrap-resilience.test.ts` | web；219行 | 是；fixture/断言/异常注入已读，未执行 | UI-004 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/bootstrap.ts` | web；173行 | 是；实现/调用/条件/持久状态已读 | UI-004 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/diagnostics/index.ts` | web；61行 | 是；实现/调用/条件/持久状态已读 | UI-015 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/dom.ts` | web；359行 | 是；实现/调用/条件/持久状态已读 | UI-011、UI-012 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/jobs/completion-responsiveness.test.ts` | web；590行 | 是；fixture/断言/异常注入已读，未执行 | UI-012 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/jobs/index.ts` | web；1825行 | 是；实现/调用/条件/持久状态已读 |ACT-001、JOB-008、UI-009、UI-017 | D-014、D-019 |
| `web/frontend/src/jobs/log-dock.test.ts` | web；320行 | 是；fixture/断言/异常注入已读，未执行 | UI-009 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/jobs/manual-unknown-v112.test.ts` | web；28行 | 是；fixture/断言/异常注入已读，未执行 | ACT-001、ACT-013 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/jobs/participation-dedup.test.ts` | web；89行 | 是；fixture/断言/异常注入已读，未执行 |JOB-007、ACT-013、UI-017 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/jobs/rest-snapshot-freshness.test.ts` | web；239行 | 是；fixture/断言/异常注入已读，未执行 | JOB-007、ACT-013 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/main.ts` | web；8行 | 是；实现/调用/条件/持久状态已读 | UI-004 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/notifications/index.ts` | web；536行 | 是；实现/调用/条件/持久状态已读 |NTF-001、NTF-004、NTF-006 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/notifications/overview.test.ts` | web；173行 | 是；fixture/断言/异常注入已读，未执行 | NTF-001、NTF-003、NTF-004 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/notifications/scheduler-state.test.ts` | web；203行 | 是；fixture/断言/异常注入已读，未执行 | NTF-004、UI-010 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/notifications/status-labels.test.ts` | web；23行 | 是；fixture/断言/异常注入已读，未执行 | NTF-004、UI-010 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/overview.test.ts` | web；27行 | 是；fixture/断言/异常注入已读，未执行 | UI-004 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/profiles/index.test.ts` | web；269行 | 是；fixture/断言/异常注入已读，未执行 | SYS-005、SYS-007 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/profiles/index.ts` | web；529行 | 是；实现/调用/条件/持久状态已读 | SYS-003、SYS-005、SYS-007 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/profiles/onboarding.test.ts` | web；626行 | 是；fixture/断言/异常注入已读，未执行 | UI-012、SYS-003 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/realtime/job-freshness.test.ts` | web；80行 | 是；fixture/断言/异常注入已读，未执行 | JOB-008 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/realtime/sse.ts` | web；269行 | 是；实现/调用/条件/持久状态已读 | JOB-008 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/realtime/terminal-snapshot.test.ts` | web；49行 | 是；fixture/断言/异常注入已读，未执行 | JOB-008 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/repost-cleanup/index.test.ts` | web；1019行 | 是；fixture/断言/异常注入已读，未执行 | CLN-005、CLN-006、CLN-007、CLN-009 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/repost-cleanup/index.ts` | web；1455行 | 是；实现/调用/条件/持久状态已读 | CLN-005、CLN-008、CLN-009 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/runtime/index.ts` | web；105行 | 是；实现/调用/条件/持久状态已读 | UI-016 | D-012 |
| `web/frontend/src/settings/index.ts` | web；416行 | 是；实现/调用/条件/持久状态已读 | ACT-010、UI-014 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/settings/llm-timeout.test.ts` | web；145行 | 是；fixture/断言/异常注入已读，未执行 | UI-014 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/shell/confirm.ts` | web；135行 | 是；实现/调用/条件/持久状态已读 | UI-012 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/shell/nav.ts` | web；110行 | 是；实现/调用/条件/持久状态已读 | UI-009、UI-011 | D-014 |
| `web/frontend/src/shell/theme.test.ts` | web；61行 | 是；fixture/断言/异常注入已读，未执行 | UI-011 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/shell/theme.ts` | web；136行 | 是；实现/调用/条件/持久状态已读 | UI-011 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/shell/toast.ts` | web；111行 | 是；实现/调用/条件/持久状态已读 | UI-012 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/sources/ds9-panel.test.ts` | web；155行 | 是；fixture/断言/异常注入已读，未执行 | UI-007 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/sources/ds9-panel.ts` | web；232行 | 是；实现/调用/条件/持久状态已读 | UI-007、UI-008 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/sources/refresh-all-entrypoints.test.ts` | web；26行 | 是；fixture/断言/异常注入已读，未执行 | UI-005 | D-013 |
| `web/frontend/src/sources/update-all.test.ts` | web；61行 | 是；fixture/断言/异常注入已读，未执行 | SC-017 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/sources/update-all.ts` | web；220行 | 是；实现/调用/条件/持久状态已读 | SC-017、UI-005 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/state.ts` | web；36行 | 是；实现/调用/条件/持久状态已读 | UI-004、JOB-008 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/styles/styles.css` | web；8302行 | 是；布局/交互状态及使用关系已读 | UI-011 | D-014 |
| `web/frontend/src/types/index.ts` | web；91行 | 是；实现/调用/条件/持久状态已读 | SYS-021 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/utils/format.test.ts` | web；234行 | 是；fixture/断言/异常注入已读，未执行 | UI-001、JOB-012、UI-010 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/utils/format.ts` | web；247行 | 是；实现/调用/条件/持久状态已读 | UI-001、JOB-012、UI-010 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/utils/motion.test.ts` | web；371行 | 是；fixture/断言/异常注入已读，未执行 | UI-011 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/utils/motion.ts` | web；208行 | 是；实现/调用/条件/持久状态已读 | UI-011 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/utils/text.ts` | web；41行 | 是；实现/调用/条件/持久状态已读 | UI-012 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/src/watch/index.test.ts` | web；58行 | 是；fixture/断言/异常注入已读，未执行 | UI-005、UI-006 | mock覆盖有限，实际执行待验证 |
| `web/frontend/src/watch/index.ts` | web；305行 | 是；实现/调用/条件/持久状态已读 | UI-005、UI-006 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/vite.config.ts` | web；36行 | 是；实现/调用/条件/持久状态已读 | SYS-022、SYS-023 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/vitest.config.ts` | web；9行 | 是；实现/调用/条件/持久状态已读 | SYS-022 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/frontend/vitest.setup.ts` | web；25行 | 是；实现/调用/条件/持久状态已读 | SYS-022 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/job_runner.py` | web；811行 | 是；实现/调用/条件/持久状态已读 | ACT-003、JOB-006、JOB-007 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/schemas/__init__.py` | web；36行 | 是；包初始化/导出已读 | SYS-021 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/schemas/account.py` | web；9行 | 是；实现/调用/条件/持久状态已读 | UI-013、SYS-004、NTF-005 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/schemas/auto.py` | web；7行 | 是；实现/调用/条件/持久状态已读 | JOB-004 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/schemas/common.py` | web；28行 | 是；实现/调用/条件/持久状态已读 | SYS-021、JOB-012 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/schemas/diagnostics.py` | web；22行 | 是；实现/调用/条件/持久状态已读 | UI-015 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/schemas/jobs.py` | web；58行 | 是；实现/调用/条件/持久状态已读 |SYS-021、JOB-006 | D-012 |
| `web/schemas/repost_cleanup.py` | web；21行 | 是；实现/调用/条件/持久状态已读 | CLN-007 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/schemas/settings.py` | web；19行 | 是；实现/调用/条件/持久状态已读 | UI-014、ACT-010 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/schemas/updates.py` | web；19行 | 是；实现/调用/条件/持久状态已读 | UI-016 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/schemas/watch.py` | web；23行 | 是；实现/调用/条件/持久状态已读 | UI-005、UI-006 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/security.py` | web；142行 | 是；实现/调用/条件/持久状态已读 | SYS-021 | D-019 |
| `web/sse.py` | web；130行 | 是；实现/调用/条件/持久状态已读 | JOB-008 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/tray_health.py` | web；79行 | 是；实现/调用/条件/持久状态已读 | SYS-012 | 未发现独立疑点；运行行为未在本轮验证 |
| `web/user_messages.py` | web；250行 | 是；实现/调用/条件/持久状态已读 | JOB-012 | 未发现独立疑点；运行行为未在本轮验证 |

## 补充资源与非逻辑文件（27项）

| 路径 | 检查用途 |
|---|---|
| `.gitattributes` | 已读shell脚本LF约束；仓库配置，无运行Job |
| `.gitignore` | 已读敏感配置、Profile/数据根、缓存/构建产物排除规则；不执行清理 |
| `config/cookies.txt.example` | 仅4行导入格式说明、无Cookie记录；不读取真实cookies.txt |
| `config/llm.env.example` | 已检查5个配置键与共享LLM加载契约；不记录示例或真实密钥值 |
| `docs/.nojekyll` | 空文件；Pages资源处理标记，不是业务入口 |
| `docs/sitemap.xml` | 已读单官网URL的静态sitemap；不请求该URL |
| `assets/brand/icon.svg` | 已解析SVG绘图节点，无script；作为图标/页面资源而非业务逻辑 |
| `assets/star-history/star-history-dark.svg` | 已解析SVG绘图节点，无script；作为图标/页面资源而非业务逻辑 |
| `assets/star-history/star-history-light.svg` | 已解析SVG绘图节点，无script；作为图标/页面资源而非业务逻辑 |
| `docs/favicon.svg` | 已解析SVG绘图节点，无script；作为图标/页面资源而非业务逻辑 |
| `docs/images/favicon.svg` | 已解析SVG绘图节点，无script；作为图标/页面资源而非业务逻辑 |
| `docs/images/logo.svg` | 已解析SVG绘图节点，无script；作为图标/页面资源而非业务逻辑 |
| `docs/images/tour/manifest.json` | 页面tour资源清单与landing消费关系 |
| `docs/robots.txt` | 静态文本资源；无业务执行 |
| `git.txt` | 仓库文本备注，非运行入口 |
| `mcp/requirements.txt` | 依赖/正式scripts/类型配置读取，不安装升级 |
| `requirements-dev.txt` | 依赖/正式scripts/类型配置读取，不安装升级 |
| `requirements.txt` | 依赖/正式scripts/类型配置读取，不安装升级 |
| `tests/fixtures/ds9_topic_feed_modern.json` | 读取fixture结构与消费断言，未请求远端 |
| `tests/fixtures/v87_delete_risk_20.json` | 读取fixture结构与消费断言，未请求远端 |
| `tests/fixtures/v88_error_job_contract.json` | 读取fixture结构与消费断言，未请求远端 |
| `web/frontend/package-lock.json` | 锁文件JSON解析及依赖边界；不逐行审计第三方包源码 |
| `web/frontend/package.json` | 依赖/正式scripts/类型配置读取，不安装升级 |
| `web/frontend/public/favicon.svg` | 已解析SVG绘图节点，无script；作为图标/页面资源而非业务逻辑 |
| `web/frontend/tsconfig.json` | 依赖/正式scripts/类型配置读取，不安装升级 |
| `web/frontend/tsconfig.node.json` | 依赖/正式scripts/类型配置读取，不安装升级 |
| `web/static/favicon.svg` | 已解析SVG绘图节点，无script；作为图标/页面资源而非业务逻辑 |

## 未执行/无法由源码证明的范围

真实API字段当前有效性、网络/平台限制、真实中奖情况、安装包完整性/最新HEAD一致性、Explorer实际图标显示、用户安装版人工验收，均未在本轮验证。动态入口及遗留链见功能清单D-012；前端@ts-nocheck和E2E选择器差异见D-013/D-019。本报告没有0 failed/PASSED或已人工通过的宣称。

复核方法：重新git枚举比对本表路径、AST解析、API装饰器/Job白名单/HTML控件/MCP索引；检查三个报告功能ID一一对应、文件引用真实存在、状态全部未测试、无业务diff。新增源码或HEAD变化后必须增量更新，而不能沿用‘508全覆盖’作为新版本结论。


## 最终静态交付复核

新增UI-017与NTF-006属于已有源码的漏列能力/预留边界，不是新增业务。功能目录含完整扫描20问、动作15问、API/MCP/Job/HTML/动态/CLI入口及21条分级疑点。源码508行覆盖总数不变；所有人工项仍未测试。最终检查仅枚举、AST/引用校验、Markdown状态/表格校验与git只读命令，不执行源码/测试，不将静态查找当功能通过。
