# Binggo 全功能普查与源码审计

> V118 更新（2026-10-10）：下文保留 V117 只读审计快照，不代表修复后的实时状态。D-001/002/004/007/018/021 的修复、离线验证及剩余边界以 [V118 P1 修复报告](V118_P1_FIX_REPORT.md) 为准。D-018 人工例外及二次确认已获用户明确保留；其他 15 项尚未处理。人工安装/真实账号验收仍不等于自动测试。

审计基线：2026-10-09～10；`release/final-gate-package-v117`，HEAD `826cde719154765dc09cb2b116e52fbaa2787a58`。

本报告以当前实现为准，不把历史需求或测试模拟当作生产事实。完整阅读508个自有源码/脚本/测试/配置文件，补查27个非逻辑配置/资源；识别137项功能：SC23、ACT18、JOB12、UI17、CLN10、NTF6、SYS27、OPS24。入口和矩阵在目录后，疑点统一标证据等级；本轮没有执行测试或真实业务请求。

配套：[文件覆盖](PROJECT_CODE_COVERAGE.md)、[逐项验收计划](PROJECT_FEATURE_TEST_PLAN.md)。全部人工计划为未测试，不能将静态覆盖理解为实际运行或安装验收通过。

## SC 功能目录（23项）

### SC-001 DS-1 小助理最新视频描述

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-001；UP 885439 → WBI 投稿列表（失败时旧接口）→ 最新视频详情/HTML 回退 → 描述动态链接 → 公共 Pipeline |
| 触发入口 | 数据源单源更新；串行更新全部；自动 refresh；check_ds1 CLI |
| 代码位置 | src/sources/ds1_xiaozhuli.py::check_update（24）；src/sources/common.py；web/actions.py::DS_HANDLERS（322） |
| 调用关系 | UP 885439 → WBI 投稿列表（失败时旧接口）→ 最新视频详情/HTML 回退 → 描述动态链接 → 公共 Pipeline |
| 数据读写 | Profile DB source checkpoint、DS 输出快照、activities |
| 当前状态 | 在用 |
| 已有测试 | tests/test_ds_checkpoint_commit.py；tests/test_sources_common.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-001；离线替换最新视频列表、描述为 3 个 t 链接；连续运行两次，再替换视频容器。 |
| 风险/边界 | 只检查最新 1 个视频，间隔内连续发布可能漏掉中间视频；没有自动无限追溯。 |

### SC-002 DS-2 番茄薯条喵（历史模块名fanqiao）近期专栏

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-002；UP 3546836235193146 → /x/space/article 第一页最近 5 篇 → checkpoint 前的新文章 → 正文节点/URL提取 → 公共 Pipeline |
| 触发入口 | 单源更新；串行更新全部；自动 refresh；check_ds2 CLI |
| 代码位置 | src/sources/ds2_fanqiao.py::check_update（19）；src/sources/recent_articles.py::check_recent_articles（61） |
| 调用关系 | UP 3546836235193146 → /x/space/article 第一页最近 5 篇 → checkpoint 前的新文章 → 正文节点/URL提取 → 公共 Pipeline |
| 数据读写 | source checkpoint；成功容器缓存；DS快照；activities |
| 当前状态 | 在用 |
| 已有测试 | tests/test_bounded_source_history_v89.py；对应来源测试（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-002；离线提供 5 篇专栏，在第 4 篇设置旧边界；运行、重跑，并模拟边界不在第一页。 |
| 风险/边界 | 有界最近 5 篇，不是全文历史；未找到旧边界不会伪造完成，也不会自动翻第 2 页。 |

### SC-003 DS-4 J君名近期专栏

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-003；UP 126038161 → /x/space/article 第一页最近 5 篇 → checkpoint 前的新文章 → 正文节点/URL提取 → 公共 Pipeline |
| 触发入口 | 单源更新；串行更新全部；自动 refresh；check_ds4 CLI |
| 代码位置 | src/sources/ds4_junming.py::check_update（22）；src/sources/recent_articles.py::check_recent_articles（61） |
| 调用关系 | UP 126038161 → /x/space/article 第一页最近 5 篇 → checkpoint 前的新文章 → 正文节点/URL提取 → 公共 Pipeline |
| 数据读写 | source checkpoint；成功容器缓存；DS快照；activities |
| 当前状态 | 在用 |
| 已有测试 | tests/test_bounded_source_history_v89.py；对应来源测试（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-003；离线提供 5 篇专栏，在第 4 篇设置旧边界；运行、重跑，并模拟边界不在第一页。 |
| 风险/边界 | 有界最近 5 篇，不是全文历史；未找到旧边界不会伪造完成，也不会自动翻第 2 页。 |

### SC-004 DS-5 互动娘近期专栏

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-004；UP 3546776042736296 → /x/space/article 第一页最近 5 篇 → checkpoint 前的新文章 → 正文节点/URL提取 → 公共 Pipeline |
| 触发入口 | 单源更新；串行更新全部；自动 refresh；check_ds5 CLI |
| 代码位置 | src/sources/ds5_hudong.py::check_update（15）；src/sources/recent_articles.py::check_recent_articles（61） |
| 调用关系 | UP 3546776042736296 → /x/space/article 第一页最近 5 篇 → checkpoint 前的新文章 → 正文节点/URL提取 → 公共 Pipeline |
| 数据读写 | source checkpoint；成功容器缓存；DS快照；activities |
| 当前状态 | 在用 |
| 已有测试 | tests/test_bounded_source_history_v89.py；对应来源测试（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-004；离线提供 5 篇专栏，在第 4 篇设置旧边界；运行、重跑，并模拟边界不在第一页。 |
| 风险/边界 | 有界最近 5 篇，不是全文历史；未找到旧边界不会伪造完成，也不会自动翻第 2 页。 |

### SC-005 DS-6 糯米近期专栏

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-005；UP 492426375 → /x/space/article 第一页最近 5 篇 → checkpoint 前的新文章 → 正文节点/URL提取 → 公共 Pipeline |
| 触发入口 | 单源更新；串行更新全部；自动 refresh；check_ds6 CLI |
| 代码位置 | src/sources/ds6_nuomi.py::check_update（15）；src/sources/recent_articles.py::check_recent_articles（61） |
| 调用关系 | UP 492426375 → /x/space/article 第一页最近 5 篇 → checkpoint 前的新文章 → 正文节点/URL提取 → 公共 Pipeline |
| 数据读写 | source checkpoint；成功容器缓存；DS快照；activities |
| 当前状态 | 在用 |
| 已有测试 | tests/test_bounded_source_history_v89.py；对应来源测试（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-005；离线提供 5 篇专栏，在第 4 篇设置旧边界；运行、重跑，并模拟边界不在第一页。 |
| 风险/边界 | 有界最近 5 篇，不是全文历史；未找到旧边界不会伪造完成，也不会自动翻第 2 页。 |

### SC-006 DS-7 大锦鲤近期专栏

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-006；UP 226257459 → /x/space/article 第一页最近 5 篇 → checkpoint 前的新文章 → 正文节点/URL提取 → 公共 Pipeline |
| 触发入口 | 单源更新；串行更新全部；自动 refresh；check_ds7 CLI |
| 代码位置 | src/sources/ds7_dajinli.py::check_update（15）；src/sources/recent_articles.py::check_recent_articles（61） |
| 调用关系 | UP 226257459 → /x/space/article 第一页最近 5 篇 → checkpoint 前的新文章 → 正文节点/URL提取 → 公共 Pipeline |
| 数据读写 | source checkpoint；成功容器缓存；DS快照；activities |
| 当前状态 | 在用 |
| 已有测试 | tests/test_bounded_source_history_v89.py；对应来源测试（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-006；离线提供 5 篇专栏，在第 4 篇设置旧边界；运行、重跑，并模拟边界不在第一页。 |
| 风险/边界 | 有界最近 5 篇，不是全文历史；未找到旧边界不会伪造完成，也不会自动翻第 2 页。 |

### SC-007 DS-3 工具人外层专栏与嵌套合集

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-007；UP 100680137 最新 cv → 外层正文既有提取 → 明确 Opus 容器 detail 一次 → 动态叶子去重 → 公共 Pipeline |
| 触发入口 | DS-3 更新；串行全部；自动 refresh；check_ds3 |
| 代码位置 | src/sources/ds3_gongjuren.py::check_update（313） / expand_container_links |
| 调用关系 | UP 100680137 最新 cv → 外层正文既有提取 → 明确 Opus 容器 detail 一次 → 动态叶子去重 → 公共 Pipeline |
| 数据读写 | 成功/失败容器信息随 DS快照；完整 checkpoint；activities |
| 当前状态 | 在用 |
| 已有测试 | tests/test_ds3_article_expansion.py；tests/test_ds3_partial_recovery_v85.py；tests/test_ds3_source_progress.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-007；用合集 fixture 提供 55 个叶子、重复链接和一个失败合集；重跑只补失败容器。 |
| 风险/边界 | -352/-509/HTTP429 后停止同类 detail；普通已知读取失败可 skip，未知异常抛出。 |

### SC-008 DS9 多话题动态扫描

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-008；/x/polymer/web-dynamic/v1/feed/topic → 每页20、最多3页 → 原帖二次发现 → source observations → 公共 Pipeline |
| 触发入口 | DS9卡片更新；refresh_source DS-9；refresh_all/自动 refresh |
| 代码位置 | src/sources/ds9_topics.py::check_update（609）；web/actions.py |
| 调用关系 | /x/polymer/web-dynamic/v1/feed/topic → 每页20、最多3页 → 原帖二次发现 → source observations → 公共 Pipeline |
| 数据读写 | Profile话题 JSON；每话题 fingerprint/checkpoint；activities；贡献审计 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_ds9_topics_m1.py；tests/test_ds9_topics_m1.py（incomplete 及缓存断言）（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-008；模拟三个话题、60条动态、相同完整头部/不完整头部；关闭一个话题再更新。 |
| 风险/边界 | 只是最多3页有界窗口；移动头部超出范围仍可能遗漏旧内容；没有跨所有账号的统一去重。 |

### SC-009 Following 关注动态最新窗口

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-009；feed/all(type=all,retries=0) → 验证页/offset → 排除本人动态 → 包装动态与一层原帖分别候选 → 25条批次 Pipeline |
| 触发入口 | 数据源立即扫描关注动态；自动 following；Job following_feed_scan |
| 代码位置 | src/following_feed.py::scan_following_feed（622） |
| 调用关系 | feed/all(type=all,retries=0) → 验证分页/offset（仅旧无分页字段首屏兼容30项上限）→ 排除本人动态 → 包装动态与一层原帖分别候选 → 25条批次 Pipeline |
| 数据读写 | following_feed_state.json v2；activities；源快照/统计 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_following_feed.py；tests/test_following_feed_v97.py；tests/test_following_batch_persistence_v115.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-009；离线首次单页，随后插入新头部、旧边界跨页和20页上限。 |
| 风险/边界 | 最新窗口最多20页；某些读取失败返回 success 但不提交 checkpoint，不能仅靠 UI 完成判断全覆盖。 |

### SC-010 Following 历史补漏与 recent catch-up

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-010；最新窗口 → recent 待追赶最多5页 → 独立15天 history待追溯最多5页 → 可靠批次处理 → 条件更新双游标 |
| 触发入口 | 同一 following_feed_scan 内部按持久状态自动继续 |
| 代码位置 | src/following_feed.py::scan_following_feed（622） |
| 调用关系 | 最新窗口 → recent 待追赶最多5页 → 独立15天 history待追溯最多5页 → 可靠批次处理 → 条件更新双游标 |
| 数据读写 | following_feed_state.json 的 recent/history offset、锚点、固定边界、完成状态 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_following_feed_v97.py；tests/test_following_feed_checkpoint_v16.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-010；使用新增头部加未完旧历史 fixture；注入重复offset、断网、重启。 |
| 风险/边界 | 15天范围之外不保证发现；分页重读是保守恢复成本，不等于重复参与。 |

### SC-011 Following 每25条分类落盘

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-011；整轮候选收集 → 25条串行分类批次 → append_activities commit → 下一批；共享canonical集合/LLMbreaker |
| 触发入口 | Following 候选收集后内部调用 |
| 代码位置 | src/following_feed.py::_run_pipeline_batches（555）；src/pipeline/refresh_all_pipeline.py::PipelineBatchContext（89） |
| 调用关系 | 整轮候选收集 → 25条串行分类批次 → append_activities commit → 下一批；共享canonical集合/LLMbreaker |
| 数据读写 | activities；进程内批次上下文；窗口 checkpoint仅最终连续覆盖后更新 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_following_batch_persistence_v115.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-011；400候选整批/分批对照；第3批SSL EOF及第3批DB写失败；新进程重跑。 |
| 风险/边界 | 分页获取阶段失败尚未进入分类；非抽奖未永久缓存，失败批次及非抽奖仍可重复分类。 |

### SC-012 WATCH 指定UP转发监控

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-012；/feed/space(host_mid) → 原始FORWARD的orig.id_str → 最近10天/本轮边界 → 公共 Pipeline |
| 触发入口 | WATCH添加列表；刷新监控动态；自动refresh第二步 |
| 代码位置 | src/watch_feed.py::collect_forward_links_for_user（208）；src/watch_sync.py；web/actions.py::run_action（1219） |
| 调用关系 | /feed/space(host_mid) → 原始FORWARD的orig.id_str → 最近10天/本轮边界 → 公共 Pipeline |
| 数据读写 | watch_users；watch_resume.json；WATCH checkpoint；activities |
| 当前状态 | 在用 |
| 已有测试 | tests/test_watch_users.py；tests/test_watch_truncated_resume_v86.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-012；用两个监控UP、原创和转发混合 fixture；跨页和30页上限。 |
| 风险/边界 | 不是完整关注列表，也不分类UP所有原创；与Following/DS有重叠。 |

### SC-013 WATCH 超上限与失败断点续扫

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-013；固定窗口+每UP offset/complete+pending links → 下一轮先新头再旧断点 → Pipeline → 条件退役resume |
| 触发入口 | refresh_watch内部根据 watch_resume.json 继续 |
| 代码位置 | src/watch_resume.py；src/watch_feed.py::collect_forward_links_for_user（208） |
| 调用关系 | 固定窗口+每UP offset/complete+pending links → 下一轮先新头再旧断点 → Pipeline → 条件退役resume |
| 数据读写 | 原子 JSON resume + DB WATCH时间 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_watch_truncated_resume_v86.py（35/95页、写失败、插入新头）（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-013；35页首轮30页、次轮余页；注入分页失败和checkpoint写后退役失败。 |
| 风险/边界 | offset依赖平台分页，循环/畸形fail-closed；不能以页面‘完成’替代source_complete检查。 |

### SC-014 Winner @提及和回复中奖线索

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-014；/x/msgfeed/at、reply → 每页20最多20页 → 本地关键词/黑名单 → 稳定消息键 → 候选与通知准备 → 独立游标 |
| 触发入口 | 中奖通知立即深检；自动 winner；Job winner_deep_scan |
| 代码位置 | src/winner_scan/service.py::WinnerScanService.run（1332）/_fetch_feed_window（815）；src/message_api.py |
| 调用关系 | /x/msgfeed/at、reply → 每页20最多20页 → 本地关键词/黑名单 → 稳定消息键 → 候选与通知准备 → 独立游标 |
| 数据读写 | winner_scan_state.json、winner_candidates.json；通知outbox |
| 当前状态 | 在用 |
| 已有测试 | tests/test_winner_deep_scan_m3.py；tests/test_winner_cursor_v83.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-014；mock @与回复各新增40条、旧边界、30页积压；重启追赶。 |
| 风险/边界 | 不走活动分类Pipeline、不调用LLM；通知可能外部HTTP；词匹配存在误报/漏报。 |

### SC-015 Winner 未读私信深检

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-015；私信sessions最新20、最多20页 → 每未读会话消息20、最多20页 → 过滤系统/明确自动回复 → 关键词候选 |
| 触发入口 | 同一 Winner Job 私信阶段 |
| 代码位置 | src/winner_scan/service.py::_collect_dm_sessions（1261）/_fetch_dm_session_window（980）/_scan_dm_source（1141） |
| 调用关系 | 私信sessions最新20、最多20页 → 每未读会话消息20、最多20页 → 过滤系统/明确自动回复 → 关键词候选 |
| 数据读写 | 每会话seqno checkpoint；候选；过滤自动回复键；昵称7天缓存 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_winner_incremental_v70.py；tests/test_winner_auto_reply_v53.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-015；mock30个会话、多页消息、感谢关注自动回复、真领奖私信和分页缺失。 |
| 风险/边界 | 昵称补充请求遇正式风险只停止昵称查找而吞异常，是否应全局暂停另见疑点；会话列表hard cap未持久resume。 |

### SC-016 Winner 候选通知中断恢复

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-016；候选+preparation同一原子落盘 → 持久claim全部渠道 → 外部HTTP → terminal → source checkpoint |
| 触发入口 | Winner run启动先恢复；新候选dispatch |
| 代码位置 | src/winner_scan/service.py::_save_new_candidates（1537）/_dispatch_prepared（1554）；src/notification/service.py |
| 调用关系 | 候选+preparation同一原子落盘 → 持久claim全部渠道 → 外部HTTP → terminal → source checkpoint |
| 数据读写 | Profile候选 JSON；通知claim与outbox/tombstones |
| 当前状态 | 在用 |
| 已有测试 | tests/test_winner_notification_recovery_v84.py；tests/test_winner_deep_scan_m3.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-016；在候选保存、claim、HTTP、terminal各点中断；重启只恢复pending preparation。 |
| 风险/边界 | 这是通知恢复，不是B站参与重试；超过500候选的保留方向另列风险。 |

### SC-017 串行更新所有数据源

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-017；页面一键更新全部注册数据源，复用单源完整链路并逐源汇总 |
| 触发入口 | 数据源‘更新全部’；Job update_all_datasources |
| 代码位置 | web/actions.py::_update_all_datasources（870）；web/frontend/src/sources/update-all.ts |
| 调用关系 | 按当前 DS_HANDLERS 的 DS-1→2→3→4→5→6→7→9 串行检查、分类、详情、入库和条件checkpoint；完成一个源再处理下一个 |
| 数据读写 | 每源快照、activities、checkpoint、Job摘要 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_update_all_datasources.py；web/frontend/src/sources/update-all.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-017；第2源普通失败，第3源成功，再测试第3源正式风险与取消。 |
| 风险/边界 | 当前注册8个来源，包括DS9、没有DS8；函数注释仍写DS1～7，不能按旧注释缩小实际范围。普通单源失败继续，正式风险/取消停止后续源。 |

### SC-018 自动刷新批次及内部 refresh_all

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-018；refresh_all 检查注册来源并处理 → refresh_watch → refresh_status；阶段失败/风险依既有契约处理 |
| 触发入口 | 自动refresh槽位；CLI/MCP旧refresh_all；后台 run_action |
| 代码位置 | web/actions.py::run_action（1219）；web/auto_scheduler.py::REFRESH_STEPS（65）；src/pipeline/refresh_all_pipeline.py::run_refresh_all_pipeline（1062） |
| 调用关系 | refresh_all 检查注册来源并处理 → refresh_watch → refresh_status；阶段失败/风险依既有契约处理 |
| 数据读写 | source outputs/state；activities；Job/slot状态 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_auto_scheduler.py；tests/test_scheduler_resilience_v24.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-018；模拟一轮各阶段请求、普通断网、formal risk、Runner忙。 |
| 风险/边界 | 与UI串行更新全部入口范围不同；旧CLI/MCP接口需核对版本和端口。 |

### SC-019 动态URL规范化与跨源去重

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-019；t19/opus18～19 ID → 已有活动/aliases/guard过滤 → 分类后的canonical最终并发复查 → append |
| 触发入口 | 所有公共Pipeline及容器链接提取 |
| 代码位置 | src/sources/common.py::normalize_activity_id（99）；src/canonical_identity.py；src/pipeline/refresh_all_pipeline.py::_dedupe_new_dynamic_ids（195） |
| 调用关系 | t19/opus18～19 ID → 已有活动/aliases/guard过滤 → 分类后的canonical最终并发复查 → append |
| 数据读写 | 活动ID与alias；参与guard；批次seen集合 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_v33_canonical_identity.py；tests/test_pipeline_db_reads_v91.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-019；同ID不同URL、不同包装动态、跨批次/来源同时观察。 |
| 风险/边界 | 未发现通用b23短链展开；av/bv/cv不能当dynamicID；不是跨账号共享数据。 |

### SC-020 转发包装与原帖一层二次发现

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-020；FORWARD外层独立候选 → 可靠embedded orig或detail补读(retries=0) → 一层原帖另候选 → 各自分类 |
| 触发入口 | 仅Following、DS9明确转发结构 |
| 代码位置 | src/repost_origin_discovery.py::discover_origin（52）；src/pipeline/classify_fetch_context.py |
| 调用关系 | FORWARD外层独立候选 → 可靠embedded orig或detail补读(retries=0) → 一层原帖另候选 → 各自分类 |
| 数据读写 | 原帖detail缓存、source observations、activities |
| 当前状态 | 在用 |
| 已有测试 | tests/test_repost_origin_secondary_v107.py；tests/test_v33_canonical_identity.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-020；包装普通/原帖抽奖、包装抽奖/原帖普通、多包装同原帖、坏orig、风险。 |
| 风险/边界 | 普通来源canonical归一与Following/DS9双候选并非同一策略，不能随意合并。 |

### SC-021 抽奖判型与坏动态跳过

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-021；detail → 失效/充电排除 → 官方notice互动 → reserve预约 → 普通转发LLM判断 → 非抽奖skip |
| 触发入口 | 所有活动发现Pipeline；清理评估复用部分分类 |
| 代码位置 | src/pipeline/classify_step.py::classify_new_link（162）；src/lottery_classifier.py；src/lottery_api.py |
| 调用关系 | detail → 失效/充电排除 → 官方notice互动 → reserve预约 → 普通转发LLM判断 → 非抽奖skip |
| 数据读写 | 判型结果；坏动态/失败ledger；分类cache |
| 当前状态 | 在用 |
| 已有测试 | tests/test_refresh_all_bad_dynamic.py；tests/test_lottery_classifier_upower.py；tests/test_classify_forward_lottery.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-021；官方互动、预约、普通转发、普通动态、404、坏正文、正式风险混合。 |
| 风险/边界 | 官方信息与LLM文本证据等级不同；普通转发不能用LLM证明中奖结果完整。 |

### SC-022 活动详情补齐、开奖时间与入库

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-022；复用detail/notice缓存 → 奖品/作者/参与要求/可靠时间 → 初始状态 → DB最终查重提交 |
| 触发入口 | 判型成功后 enrich / status / append |
| 代码位置 | src/lottery_enricher.py；src/pipeline/enrich_fetch_context.py；src/pipeline/status_step.py；src/activity_store.py::append_activities（203） |
| 调用关系 | 复用detail/notice缓存 → 奖品/作者/参与要求/可靠时间 → 初始状态 → DB最终查重提交 |
| 数据读写 | activities结构化字段与原始JSON；LLM parse cache |
| 当前状态 | 在用 |
| 已有测试 | tests/test_sqlite_data_layer.py；tests/test_pipeline_persisted_state.py；tests/test_enrich_fetch_context.py；tests/test_enrich_skip.py；tests/test_lottery_time.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-022；缺日期、跨年日期、官方结束、并发插入相同canonical；DB异常。 |
| 风险/边界 | public Pipeline单次末尾写库，只有Following当前拆25批；其他一次内存结果仍可能遇中途失败重做。 |

### SC-023 本地活动状态与即将开奖提醒

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SC-023；有效开奖时间+本地明确参与 → ended/soon → 活动状态/提醒快照；无远端中奖核查 |
| 触发入口 | 刷新活动状态；自动refresh第三步；summary/filters |
| 代码位置 | src/status_refresh.py::refresh_local_activity_statuses（155）；src/activity_status.py::resolve_activity_status（31）；src/draw_reminder.py::compute_draw_reminders（93） |
| 调用关系 | 有效开奖时间+本地明确参与 → ended/soon → 活动状态/提醒快照；无远端中奖核查 |
| 数据读写 | activities、participations、draw_reminder snapshot |
| 当前状态 | 在用 |
| 已有测试 | tests/test_status_refresh.py；tests/test_draw_reminder.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SC-023；临近3天、过期、未参加、时间缺失、充电排除。 |
| 风险/边界 | ‘开奖提醒’不等于知道中奖；推荐@check函数当前固定False。 |

## ACT 功能目录（18项）

### ACT-001 普通人工完整参与

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | ACT-001；UI明确点击→冻结Profile/UID→Job互斥→per-target跨进程gate→本地查重→preflight→完整动作→状态保存 |
| 触发入口 | 活动行统一‘参与’→POST /api/jobs participate |
| 代码位置 | web/frontend/src/jobs/index.ts::participationClickParams（23）；web/app.py::api_start_job（1172）；src/participation.py::participate_activity（1159） |
| 调用关系 | UI明确点击→冻结Profile/UID→Job互斥→per-target跨进程gate→本地查重→preflight→完整动作→状态保存 |
| 数据读写 | participations/action日志；repost DB guard；favorite/comment JSON guard；人工尝试审计 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_unknown_manual_retry_v112.py；web/frontend/src/jobs/manual-unknown-v112.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 ACT-001；mock普通抽奖与收藏UNKNOWN后续未转发案例；点击一次‘参与’。 |
| 风险/边界 | 手工点击代表本次授权，不代表风险暂停或身份检查可绕过。 |

### ACT-002 三连目标选择与最多3个活动

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | ACT-002；DB全部可列活动→统一filter→类型/状态/时间/guard/失败backoff筛选→最多3目标→同一Job有限workers |
| 触发入口 | 活动三连按钮；自动participate_triple；预览API |
| 代码位置 | web/activity_service.py::_filtered_activity_rows（335）/_pick_triple_from_rows（442）；web/actions.py |
| 调用关系 | DB全部可列活动→统一filter→类型/状态/时间/guard/失败backoff筛选→最多3目标→同一Job有限workers |
| 数据读写 | activities、participations、失败记录、guard；候选预览不清除保护 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_participation_guards.py；tests/test_v36_remove_confirmation_ui.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 ACT-002；新旧有效活动、过期/未知/已参加、筛选排序混合；刷新后重算。 |
| 风险/边界 | 未参加展示不等于三连有资格；pending/unknown仅人工行可用；suspected仍受阻。 |

### ACT-003 自动三连参与

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | ACT-003；240～420秒抖动→remote gate→忙则跳过/延后→真正接受Job后running→最多3目标 |
| 触发入口 | Scheduler participate→Runner source=auto |
| 代码位置 | web/auto_scheduler.py::_run_triple_slot（1199）；web/job_runner.py::try_start（227）；web/actions.py::run_action（1219） |
| 调用关系 | 240～420秒抖动→remote gate→忙则跳过/延后→真正接受Job后running→最多3目标 |
| 数据读写 | scheduler_slots.json、auto_remote_state.json、参与状态 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_scheduler_jitter_v38.py；tests/test_auto_scheduler_running_state_v102.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 ACT-003；fake clock静默、Runner忙、全UNKNOWN候选、正式风险、普通候选。 |
| 风险/边界 | 3个target可同Job并行，任务互斥不是网络零并发。 |

### ACT-004 点赞动态

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | ACT-004；目标交互状态→/x/dynamic/feed/operate/thumb JSON thumb_up=1→code0/已赞语义 |
| 触发入口 | 普通互动/转发完整参与；已点赞则跳过 |
| 代码位置 | src/lottery_actions.py::like_dynamic（671） |
| 调用关系 | 目标交互状态→/x/dynamic/feed/operate/thumb JSON thumb_up=1→code0/已赞语义 |
| 数据读写 | 动作日志/上下文快照；没有专属长久unknown表 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_participation_interactions.py；tests/test_participation_success.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 ACT-004；mock已赞、未赞、code0、业务拒绝和超时；核对参数。 |
| 风险/边界 | 该动作仍用客户端默认有限重试，不可误写为所有POST retries=0。 |

### ACT-005 关注UP主

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | ACT-005；作者UID关系→/x/relation/modify act=1→code0/22014 |
| 触发入口 | 完整参与与预约流程；本地/远端已关注则跳过 |
| 代码位置 | src/lottery_actions.py::follow_user（694） |
| 调用关系 | 作者UID关系→/x/relation/modify act=1→code0/22014 |
| 数据读写 | 动作日志/上下文；没有独立follow持久unknown屏障 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_participation_interactions.py；tests/test_login_identity_guard_v8.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 ACT-005；mock已关注、错误UID、未关注、重复业务码。 |
| 风险/边界 | 关注与点赞为明确幂等意图，重试契约不同于评论/转发。 |

### ACT-006 收藏目标与持久UNKNOWN屏障

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | ACT-006；可靠UID+目标对象→远端收藏状态→pending先落盘→cosmo favorite POST(retries=0)→可靠响应+再查证→confirmed/unknown |
| 触发入口 | 完整参与 favorite 步骤 |
| 代码位置 | src/lottery_actions.py::favorite_dynamic（711）；src/favorite_confirmation.py |
| 调用关系 | 可靠UID+目标对象→远端收藏状态→pending先落盘→cosmo favorite POST(retries=0)→可靠响应+再查证→confirmed/unknown |
| 数据读写 | favorite_confirmation Profile JSON、锁；兼容保留的action日志unknown |
| 当前状态 | 在用 |
| 已有测试 | tests/test_favorite_guard_v98.py；tests/test_favorite_optional.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 ACT-006；状态GET失败、pending写失败、POST超时、成功后GET不明、日志裁剪、重启、并发。 |
| 风险/边界 | 旧unknown已裁剪且没有guard时无法凭空还原；本轮人工授权只针对可识别旧记录。 |

### ACT-007 转发动态与长期防重

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | ACT-007；local confirmed→0查重请求；否则精确目标检查→pending DB commit→/x/dynamic/feed/create form→code0立即confirmed→后续评论 |
| 触发入口 | 完整参与 repost 步骤；官方已转发正向证据 |
| 代码位置 | src/lottery_actions.py::repost_dynamic（823）；src/participation_guard.py |
| 调用关系 | local confirmed→0查重请求；否则精确目标检查→pending DB commit→/x/dynamic/feed/create form→code0立即confirmed→后续评论 |
| 数据读写 | participation_guard(uid,dynamic_id)；整体participations另存；dirty history同步标记 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_participation_guard.py；tests/test_v5_participation_isolation.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 ACT-007；pending写失败、超时、code0后评论失败、重启/跨进程同目标。 |
| 风险/边界 | 不存在可靠通用‘已转发’GET；点赞+收藏不能伪造成已参与。 |

### ACT-008 普通评论持久防重与诊断

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | ACT-008；可靠评论rid/type与UID→pending JSON durable→/x/v2/reply/add retries0→code0/12051 confirmed；不明 unknown |
| 触发入口 | 完整参与 comment；comment recovery probe受控调用 |
| 代码位置 | src/lottery_actions.py::comment_dynamic（886）；src/comment_confirmation.py |
| 调用关系 | 可靠评论rid/type与UID→pending JSON durable→/x/v2/reply/add retries0→code0/12051 confirmed；不明 unknown |
| 数据读写 | comment_confirmed.json v2按UID+dynamic；legacy confirmed整个Profile阻挡；安全诊断 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_comment_mutation_guard_v81.py；tests/test_comment_unknown_diagnostics_v104.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 ACT-008；POST超时/响应破损、confirmed写失败、跨进程竞争、旧无UID confirmed。 |
| 风险/边界 | 12014另触发评论暂停；普通网络错误不升级为formal risk。 |

### ACT-009 预约抽奖

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | ACT-009；reserve结构/已预约状态→关注→reserve相关POST(retries=0)→整体参与记录 |
| 触发入口 | 预约类型完整参与 |
| 代码位置 | src/participation.py::_reserve_click（774）；src/lottery_api.py |
| 调用关系 | reserve结构/已预约状态→关注→reserve相关POST(retries=0)→整体参与记录 |
| 数据读写 | 预约状态与参与日志；无独立长期reserve mutation guard |
| 当前状态 | 在用 |
| 已有测试 | tests/test_reserve_no_retry_v9.py；tests/test_classify_reserve_live.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 ACT-009；mock已预约、未预约、超时、LIVE预约排除。 |
| 风险/边界 | 预约超时没有comment/favorite/repost同等专属长期屏障，需独立评估，不能宣传全部mutation永久幂等。 |

### ACT-010 参与文案：自定义与随机评论

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | ACT-010；自定义/随机评论模式→有限评论GET池→排除不合适内容→233字限制→fallback好运连连 |
| 触发入口 | 概览文案设置；完整参与自动读取 |
| 代码位置 | src/participate_text.py::resolve_participate_text_for_activity（130）；src/user_settings.py；web/frontend/src/settings/index.ts |
| 调用关系 | 自定义/随机评论模式→有限评论GET池→排除不合适内容→233字限制→fallback好运连连 |
| 数据读写 | UID用户设置；文本选择只在本次流程复用 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_participate_text.py；tests/test_participate_text.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 ACT-010；mock空评论池、3页20条、有文案、fallback；切换模式保存。 |
| 风险/边界 | 随机模式增加有限远端只读请求；不能把其他人评论视作成功证据。 |

### ACT-011 目标交互预检查与账号身份核验

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | ACT-011；本地明确参加优先→只读当前目标detail/notice/收藏/关系→失败fence→禁止后续冒险 |
| 触发入口 | 所有完整参与在动作前 |
| 代码位置 | src/participate_preflight.py；src/lottery_actions.py::ParticipationReadClient（160）；src/bilibili_auth.py |
| 调用关系 | 本地明确参加优先→只读当前目标detail/notice/收藏/关系→失败fence→禁止后续冒险 |
| 数据读写 | 进程内本次缓存；可靠平台状态；无个人历史实时查重 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_participation_guards.py；tests/test_login_identity_guard_v8.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 ACT-011；local参加时断言0目标GET；未知目标GET失败后断言0POST。 |
| 风险/边界 | 普通转发远端不完整证据默认suspected；不是HTTP成功就可推定动作成功。 |

### ACT-012 全生命周期跨进程参与gate

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | ACT-012；实际runtime DB路径+UID+dynamicID hash→OS锁→本地/远端检查→所有动作→保存→释放 |
| 触发入口 | Web、自动、participate CLI进入核心 |
| 代码位置 | src/participation_guard.py；src/participation.py::participate_activity（1159） |
| 调用关系 | 实际runtime DB路径+UID+dynamicID hash→OS锁→本地/远端检查→所有动作→保存→释放 |
| 数据读写 | Profile锁目录；SQLite仅短事务，不在网络期间持有写事务 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_participation_guard.py；tests/test_comment_mutation_guard_v81.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 ACT-012；两个独立进程同目标竞争；不同Profile/UID/目标并行。 |
| 风险/边界 | gate不能覆盖直接调用旧底层工具的所有人为入口；入口表说明边界。 |

### ACT-013 人工UNKNOWN恢复完整Pipeline

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | ACT-013；UI单次授权冻结身份→generation/audit保留原unknown→只重试本次可归属pending/unknown动作→继续未执行步骤 |
| 触发入口 | 普通参与click携带manual_unknown_retry=true；inspection GET只读 |
| 代码位置 | src/manual_participation_retry.py::retry_unknown_participation（61）；src/manual_unknown_retry.py |
| 调用关系 | UI单次授权冻结身份→generation/audit保留原unknown→只重试本次可归属pending/unknown动作→继续未执行步骤 |
| 数据读写 | manual_unknown_retry.json独立attempts；原guard不删除；pending crash fail-closed |
| 当前状态 | 在用 |
| 已有测试 | tests/test_unknown_manual_retry_v112.py；web/frontend/src/jobs/manual-unknown-v112.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 ACT-013；收藏unknown→确认like/follow已成功→收藏恢复成功→转发/评论继续；再次unknown后再点一次。 |
| 风险/边界 | legacy评论无持久可验证身份、suspected不能直接放行；用户需自己判断重复风险。 |

### ACT-014 旧单动作人工重试实现

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | ACT-014；显式库函数 → 指定动作、generation/Profile检查 → durable attempt与gate → 单动作；仅测试证实调用 |
| 触发入口 | 内部 retry_unknown_action；当前Web参与/人工Job实际都调用完整Pipeline，不调用此旧函数 |
| 代码位置 | src/manual_participation_retry.py::retry_unknown_action（99）；web/actions.py |
| 调用关系 | 显式库函数 → 指定动作、generation/Profile检查 → durable attempt与gate → 单动作；仅测试证实调用 |
| 数据读写 | 与现有guard及manual审计共享 |
| 当前状态 | 疑似遗留：函数及离线测试仍在；生产调用者未找到 |
| 已有测试 | tests/test_unknown_manual_retry_v112.py（早期单动作合同）（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 ACT-014；只离线调用旧action，核对当前UI不会要求文本或phrase。 |
| 风险/边界 | 旧单动作函数需要手动文案；不是当前页面体验。不要仅因函数存在就宣称用户仍要输入文案。 |

### ACT-015 历史账本纯本地收敛旧guard

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | ACT-015；当前UID+original exact+可信本人repost历史→pending/unknown/suspected单向confirmed；deleted tombstone仍有效 |
| 触发入口 | 历史同步成功后自动内部执行 |
| 代码位置 | src/participation_guard.py::reconcile_uncertain_guards_from_history（235） |
| 调用关系 | 当前UID+original exact+可信本人repost历史→pending/unknown/suspected单向confirmed；deleted tombstone仍有效 |
| 数据读写 | participation_guard与repost_history；不读cleanup assessment |
| 当前状态 | 在用 |
| 已有测试 | tests/test_v5_history_guard_reconciliation.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 ACT-015；240旧guard、200 exact history、不同UID/原帖/不可信history/已deleted/缺history。 |
| 风险/边界 | 账本必须可信来源/identity字段完整；不是根据标题时间猜参与。 |

### ACT-016 参与日志、失败backoff与裁剪

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | ACT-016；动作结果+整体状态→participation_actions/participations→有限日志保留→失败候选短backoff |
| 触发入口 | 每次结果保存；候选过滤读取 |
| 代码位置 | src/participation_log.py::append_action_record（100）；src/participation_store.py；src/participation_errors.py |
| 调用关系 | 动作结果+整体状态→participation_actions/participations→有限日志保留→失败候选短backoff |
| 数据读写 | 动作日志会裁剪；comment/favorite/repost持久guard独立不裁剪 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_participation_dedup_flow.py；tests/test_favorite_guard_v98.py；tests/test_scheduler_jitter_v38.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 ACT-016；裁剪action记录后重启；未知guard保留；普通失败等1小时后重算。 |
| 风险/边界 | 已经在引入guard前被裁剪的旧不明记录无法完整恢复。 |

### ACT-017 评论12014暂停与受控恢复探测

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | ACT-017；12014→本地pause→到时且Runner空闲/remote gate允许→挑安全目标→真正评论POST探测→成功解除/unknown隔离 |
| 触发入口 | 评论错误→breaker；Scheduler空闲probe |
| 代码位置 | src/comment_probe.py::run_comment_recovery_probe（30）；src/comment_breaker.py；web/auto_scheduler.py |
| 调用关系 | 12014→本地pause→到时且Runner空闲/remote gate允许→挑安全目标→真正评论POST探测→成功解除/unknown隔离 |
| 数据读写 | Profile comment_breaker JSON持久claim/探测排除集合；成功confirm_comment；没有复用普通评论POST前pending屏障 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_comment_probe_v51.py；tests/test_comment_probe_busy_gate_v101.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 ACT-017；fake clock小时probe、Runner忙、风险暂停、probe再次12014/unknown。 |
| 风险/边界 | 探测是真实评论mutation；每小时可换安全目标。probe异常中的formal传播及clear后旧unknown保护不能套用普通评论保证，见D-021，需离线专项复现。 |

### ACT-018 正式风控与非幂等UNKNOWN优先级

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | ACT-018；仅-352/-509/HTTP429→结构化风险→Job传播→Profile20分钟冷却；网络/LLM域不混淆 |
| 触发入口 | client/分类/参与/Job/自动远端gate |
| 代码位置 | src/platform_risk.py::risk_code_of（61）；src/bilibili_client.py；web/auto_remote_state.py |
| 调用关系 | 仅-352/-509/HTTP429→结构化风险→Job传播→Profile20分钟冷却；网络/LLM域不混淆 |
| 数据读写 | auto_remote_state.json；Job error字段；guard保守状态 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_formal_risk_propagation_v82.py；tests/test_api_errors.py；tests/test_user_messages.py；tests/test_risk_semantics_v13.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 ACT-018；注入三formal码以及-315/-412/-799、LLM429、SSL EOF。 |
| 风险/边界 | 遗留CLI及Winner昵称补齐有不同异常处理，另列疑点，不能宣称全项目一致。 |

## JOB 功能目录（12项）

### JOB-001 自动调度启动、停止与代际保护

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | JOB-001；登录/LLM ready→remote gate→受管线程；stop设置本代event，旧回调无权创建新Job |
| 触发入口 | Web auto start/stop；服务启动配置 |
| 代码位置 | web/auto_scheduler.py::AutoScheduler.start（540）/stop（573）；web/auto_config.py |
| 调用关系 | 登录/LLM ready→remote gate→受管线程；stop设置本代event，旧回调无权创建新Job |
| 数据读写 | Scheduler运行内存；持久slots；本地设置 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_auto_scheduler_lifecycle.py；tests/test_scheduler_orchestration_m8.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 JOB-001；fake Runner重复start、stop后马上start、旧tick返回。 |
| 风险/边界 | 停止调度不等于结束当前Job或服务。 |

### JOB-002 固定扫描槽位与扫描优先

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | JOB-002；refresh07:10/12:40/18:10/22:40；following08/11/14/17/20/23；winner12:10/22:10；依次Runner接收 |
| 触发入口 | AutoScheduler loop |
| 代码位置 | web/auto_scheduler.py::_loop（931） / _run_refresh_batch / _run_following_scan / _run_winner_scan；web/auto_scheduler.py::REFRESH_STEPS（65） |
| 调用关系 | refresh07:10/12:40/18:10/22:40；following08/11/14/17/20/23；winner12:10/22:10；依次Runner接收 |
| 数据读写 | scheduler_slots.json、内存phase/tasks |
| 当前状态 | 在用 |
| 已有测试 | tests/test_scheduler_cadence_v26.py；tests/test_scheduler_day_night_v72.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 JOB-002；fake clock三个扫描到点，自动参与也到点，Runner开始扫描。 |
| 风险/边界 | Runner忙的槽位可能消费并跳过，不是通用排队或错过分钟无限补跑。 |

### JOB-003 V116多Profile 15分钟动态错峰

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | JOB-003；V109 registry真实alive且ready→实例启动时间稳定排序→rank×15分钟→只shift refresh/following |
| 触发入口 | 自动refresh/following due计算 |
| 代码位置 | web/auto_scheduler.py::_scan_offset_minutes（380）/_runtime_scan_slots（407） |
| 调用关系 | V109 registry真实alive且ready→实例启动时间稳定排序→rank×15分钟→只shift refresh/following |
| 数据读写 | 持久logical base槽位；registry只读身份验证；next_task |
| 当前状态 | 在用 |
| 已有测试 | tests/test_multi_profile_scan_stagger_v111.py；tests/test_multi_profile_scan_stagger_scope_v116.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 JOB-003；单/双/三实例、同timestamp tie、加入退出、任务running与done。 |
| 风险/边界 | 扫描超过15分钟仍可能跨账号重叠；每进程RPS非全局；错峰不保证不同原计划无碰撞。 |

### JOB-004 夜间静默与继续挂机开关

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | JOB-004；默认00:00～07:00禁止自动远端Job→自动参与顺延07:00后+jitter；显式night_continue允许夜间但不豁免风险 |
| 触发入口 | Web night-continue切换；Scheduler remote gate |
| 代码位置 | web/auto_config.py；web/auto_scheduler.py::_remote_gate（731） / _eligible_triple_due（1458） |
| 调用关系 | 默认00:00～07:00禁止自动远端Job→自动参与顺延07:00后+jitter；显式night_continue允许夜间但不豁免风险 |
| 数据读写 | Profile night_continue设置；next自动参与持久时间 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_night_quiet_scheduler_v99.py；tests/test_scheduler_day_night_v72.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 JOB-004；00/06:59/07:00边界；offset跨午夜；quiet中多轮tick。 |
| 风险/边界 | 开关不是全球暂停解除，也不修改BILI_RPS。 |

### JOB-005 持久调度槽位原子领取

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | JOB-005；线程/OS窄锁→读取校验→比较slot→原子replace claim→派发；先claim后请求 |
| 触发入口 | 每种自动task开始前 |
| 代码位置 | web/auto_scheduler.py::_persist_slot_claimed（179） |
| 调用关系 | 线程/OS窄锁→读取校验→比较slot→原子replace claim→派发；先claim后请求 |
| 数据读写 | scheduler_slots.json及窄Profile锁 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_scheduler_slot_atomic_v68.py；tests/test_scheduler_slot_persistence_v9.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 JOB-005；两个进程同时领取、claim后崩溃、状态损坏、写失败。 |
| 风险/边界 | 有意偏向防重复，领取后崩溃可能漏本slot；下个正常槽位继续，并非保证每轮必执行。 |

### JOB-006 Job创建、互斥与执行白名单

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | JOB-006；restart gate+source权限+业务预检→单当前Job→新cancelEvent→worker→writer lease→动作分支 |
| 触发入口 | POST /api/jobs；Scheduler；MCP；清理delete路由 |
| 代码位置 | web/job_runner.py::JobRunner.try_start（227）；web/schemas/jobs.py::ALLOWED_JOB_ACTIONS；web/actions.py::run_action（1219） |
| 调用关系 | restart gate+source权限+业务预检→单当前Job→新cancelEvent→worker→writer lease→动作分支 |
| 数据读写 | SQLite jobs；EventHub；Profile writer lease |
| 当前状态 | 在用 |
| 已有测试 | tests/test_job_runner_login.py；tests/test_auto_scheduler_guard.py；tests/test_job_store.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 JOB-006；同时提交扫描、参与、manual auth source=auto和未知action。 |
| 风险/边界 | Job内局部workers仍并行；不存在跨所有Profile全局Runner锁。 |

### JOB-007 取消、进度与可靠终态

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | JOB-007；本Job event set→业务阶段cancel_check→已落盘保留→success/error/cancelled/interrupted持久 |
| 触发入口 | 停止当前任务按钮；/api/jobs/cancel；supervisor安全退出 |
| 代码位置 | web/job_runner.py::cancel（335）；src/job_store.py |
| 调用关系 | 本Job event set→业务阶段cancel_check→已落盘保留→success/error/cancelled/interrupted持久 |
| 数据读写 | jobs/progress/log/phase/performance；崩溃运行任务恢复interrupted |
| 当前状态 | 在用 |
| 已有测试 | tests/test_job_store.py；tests/test_realtime_consistency_v2.py；tests/test_refresh_all_enrich_progress.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 JOB-007；取消分页/分类/DELETE前；终态DB写失败；重启旧running。 |
| 风险/边界 | 取消协作式，不保证立即终止网络I/O；不能强杀替代未知保护。 |

### JOB-008 SSE事件与REST恢复

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | JOB-008；snapshot→带seq事件→Job ID/authority版本→terminal吸收；断线REST只读恢复 |
| 触发入口 | /api/events；页面初始化、断线重连与fallback |
| 代码位置 | web/sse.py::sse_response（111）；web/event_hub.py；web/frontend/src/realtime/sse.ts；web/frontend/src/jobs/index.ts |
| 调用关系 | snapshot→带seq事件→Job ID/authority版本→terminal吸收；断线REST只读恢复 |
| 数据读写 | 内存事件有界；SQLite当前/历史job；前端state |
| 当前状态 | 在用 |
| 已有测试 | tests/test_sse_events.py；web/frontend/src/realtime/job-freshness.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 JOB-008；旧Job迟到事件、同Jobterminal后running、重连seq变小、REST慢返回。 |
| 风险/边界 | UI无进度不证明后台停止；历史事件不无限保留。 |

### JOB-009 重启gate与退出码75

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | JOB-009；idle检查与关闭new_work原子化→active pointer→成功响应→优雅service退出75→父进程wait→新runtime |
| 触发入口 | Profile切换确认；源码/安装版supervisor |
| 代码位置 | src/restart_control.py::RestartControl（34）；scripts/run_dashboard.py；binggo_launcher.py |
| 调用关系 | idle检查与关闭new_work原子化→active pointer→成功响应→优雅service退出75→父进程wait→新runtime |
| 数据读写 | active_profile.json；冻结runtime；锁/registry |
| 当前状态 | 在用 |
| 已有测试 | tests/test_restart_control.py；tests/test_profile_api.py；tests/test_restart_control.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 JOB-009；mock Job刚开始与切换竞态；普通退出0/9和75；未受管--serve。 |
| 风险/边界 | 未受管服务不能假宣称自动重启可用；无法idle时不强杀。 |

### JOB-010 正式风险20分钟与失败恢复

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | JOB-010；formal风险持久pause；同一次不反复延长；普通网络/预算失败非fatal；风险时保留本地status refresh |
| 触发入口 | Job structured error→auto_remote_state→所有remote gate |
| 代码位置 | web/auto_remote_state.py；web/auto_scheduler.py::_pause_all_auto_remote（701） |
| 调用关系 | formal风险持久pause；同一次不反复延长；普通网络/预算失败非fatal；风险时保留本地status refresh |
| 数据读写 | Profile auto_remote_state.json；Scheduler任务状态 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_risk_cooldown_v20.py；tests/test_scheduler_resilience_v24.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 JOB-010；formal三码、普通HTTP5xx、SSL EOF、LLM429、损坏state、冷却过期。 |
| 风险/边界 | 历史注释/CLI说明有6小时旧文案，不等于当前formal策略。 |

### JOB-011 B站每进程RPS与有范围请求预算

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | JOB-011；共享进程token bucket(default3RPS)→真实请求attempt计scope；cache/LLM不计B站；manual cleanup scope不受120/6h截断 |
| 触发入口 | BilibiliClient请求前；清理scope等 |
| 代码位置 | src/bilibili_rate_limit.py::acquire_bilibili_request_slot（122）；src/bilibili_request_budget.py；src/bilibili_client.py |
| 调用关系 | 共享进程token bucket(default3RPS)→真实请求attempt计scope；cache/LLM不计B站；manual cleanup scope不受120/6h截断 |
| 数据读写 | 内存RPS；Profile预算JSON按scope持久 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_bilibili_rate_limit.py；tests/test_request_budget_v22.py；tests/test_manual_cleanup_run_to_completion_v77.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 JOB-011；多个线程请求、缓存命中、budget耗尽、manual>120、两个scope隔离。 |
| 风险/边界 | 多账号总RPS可相加；客户端GET/幂等POST和非幂等POST重试契约各异。 |

### JOB-012 远端结构化错误、LLM熔断及局部重试

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | JOB-012；网络cause优先于正文字符串→LLM配置/temporary/provider连续3次breaker→保留partial；HTTP429正式域限定B站 |
| 触发入口 | client→Pipeline/Job/API→toast |
| 代码位置 | src/llm_resilience.py::LlmProviderBreaker（23）；src/bilibili_client.py；web/api_errors.py；web/user_messages.py |
| 调用关系 | 网络cause优先于正文字符串→LLM配置/temporary/provider连续3次breaker→保留partial；HTTP429正式域限定B站 |
| 数据读写 | error_code/retryable/risk_code；LLM fingerprint/cache |
| 当前状态 | 在用 |
| 已有测试 | tests/test_api_errors.py；tests/test_user_messages.py；tests/test_error_domain_user_messages_v88.py；tests/test_llm_resilience_v21.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 JOB-012；异常带误导风控/登录字符串；malformedLLM、90秒deadline、provider503。 |
| 风险/边界 | retry_utils通用函数会重试429，调用点必须附合适predicate；不要直接用于mutation。 |

## UI 功能目录（17项）

### UI-001 活动列表读取、筛选、搜索、分页和排序

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | UI-001；DB活动+用户状态/guard/failure→可列过滤→搜索/类型/状态/soon/heat排序→分页 |
| 触发入口 | 活动页及/api/activities |
| 代码位置 | web/activity_service.py::list_activities（631）；web/frontend/src/activities/index.ts |
| 调用关系 | DB活动+用户状态/guard/failure→可列过滤→搜索/类型/状态/soon/heat排序→分页 |
| 数据读写 | 本地DB读取；前端filters；默认20/页 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_web_api.py；web/frontend/src/activities/activity-loading.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 UI-001；只读查看旧有效/未知/已结束fixture；关键词、排序、页码。 |
| 风险/边界 | 列表可人工恢复与自动三连资格不同。 |

### UI-002 本地删除活动与永久人工忽略

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | UI-002；确认本地删除→canonical与aliases加入人工ignore→移除活动记录→后续所有source不得重入 |
| 触发入口 | 活动行删除；DELETE /api/activities/{id} |
| 代码位置 | src/manual_ignored_activities.py；web/app.py::api_delete_activity（958）；web/frontend/src/activities/index.ts |
| 调用关系 | 确认本地删除→canonical与aliases加入人工ignore→移除活动记录→后续所有source不得重入 |
| 数据读写 | manual_ignored_activities、activities；参与guard不清 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_manual_ignore_v76.py；web/frontend/src/activities/participation-guard.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 UI-002；临时DB删除有alias活动；下一source再次扫描同原帖。 |
| 风险/边界 | 删除本地活动不等于取消已执行转发。 |

### UI-003 失败记录删除与单一反馈

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | UI-003；in-flight同ID阻挡→local失败ledger删除→响应deleted true/false→独立刷新反馈 |
| 触发入口 | 失败分类行删除；DELETE /api/activity-failures/{id} |
| 代码位置 | src/activity_failures.py::delete_activity_failure（67）；web/frontend/src/activities/index.ts |
| 调用关系 | in-flight同ID阻挡→local失败ledger删除→响应deleted true/false→独立刷新反馈 |
| 数据读写 | activity_failures；不删除真实动态 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_activity_failures_v60.py；web/frontend/src/activities/delete-feedback-v103.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 UI-003；双击、deleted:false、DELETE成功但刷新失败、另一个ID并发。 |
| 风险/边界 | 只删除失败诊断，不能解除参与unknown。 |

### UI-004 活动统计与概览

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | UI-004；读活动/guard/source快照→类型/状态/近期开奖summary；各loader独立失败不阻塞SSE |
| 触发入口 | 概览stats与summary API |
| 代码位置 | web/activity_service.py::get_summary（241）；web/frontend/src/bootstrap.ts |
| 调用关系 | 读活动/guard/source快照→类型/状态/近期开奖summary；各loader独立失败不阻塞SSE |
| 数据读写 | 本地缓存、summary、currentJob基线 |
| 当前状态 | 在用 |
| 已有测试 | web/frontend/src/overview.test.ts；web/frontend/src/bootstrap-resilience.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 UI-004；summary失败、活动失败、账号失败分别注入。 |
| 风险/边界 | 本地GET可初始化/规范化数据，非所有GET都绝对不写磁盘。 |

### UI-005 数据源卡片与更新统计

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | UI-005；可见DS1～7/ WATCH→source_id Job→每源 discovered/existing/persisted反馈；DS9独立话题区域 |
| 触发入口 | 数据源区域；DS各行更新、全部更新停止 |
| 代码位置 | web/frontend/src/watch/index.ts::renderSources（254）；web/frontend/src/sources/update-all.ts |
| 调用关系 | 可见DS1～7/ WATCH→source_id Job→每源 discovered/existing/persisted反馈；DS9独立话题区域 |
| 数据读写 | sources summary/Job；DOM in-flight按钮 |
| 当前状态 | 在用 |
| 已有测试 | web/frontend/src/sources/refresh-all-entrypoints.test.ts；web/frontend/src/watch/index.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 UI-005；模拟6成功1失败与部分风险；点击停止。 |
| 风险/边界 | 旧E2E仍选择已移除refresh_all按钮，需更新测试，当前不修。 |

### UI-006 WATCH列表管理

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | UI-006；列表GET可seed候选→add需登录/name远端查询→单行DB upsert；remove仅本地 |
| 触发入口 | 输入MID添加；监控chip删除 |
| 代码位置 | src/watch_users.py；web/frontend/src/watch/index.ts；web/app.py::api_watch_users（249） |
| 调用关系 | 列表GET可seed候选→add需登录/name远端查询→单行DB upsert；remove仅本地 |
| 数据读写 | watch_users、state updated_at；旧seed候选文件 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_watch_users_storage_v12.py；web/frontend/src/watch/index.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 UI-006；mock名称查询，重复添加、删除不存在、名称失败。 |
| 风险/边界 | 添加会请求B站卡片；GET seed有本地写副作用。 |

### UI-007 DS9话题添加、删除、暂停和恢复

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | UI-007；topic ID解析→配置验证→内置不得删除/自定义可删→启停影响扫描 |
| 触发入口 | DS9横向话题卡；内置/自定义 |
| 代码位置 | web/frontend/src/sources/ds9-panel.ts::bindDs9Panel（213）；src/sources/ds9_topics.py；web/app.py::api_ds9_add_topic（643） |
| 调用关系 | topic ID解析→配置验证→内置不得删除/自定义可删→启停影响扫描 |
| 数据读写 | Profile ds9 topic config/status |
| 当前状态 | 在用 |
| 已有测试 | tests/test_ds9_topic_display_v106.py；web/frontend/src/sources/ds9-panel.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 UI-007；临时Profile增自定义、重复ID、停用内置、再恢复。 |
| 风险/边界 | 配置改变不代表立即远端扫描。 |

### UI-008 DS9独有贡献、差值与审计导出

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | UI-008；7天canonical去重并剔除其他来源重叠→每话题exact set delta；非抽奖最多100样本API/CSV仍保留 |
| 触发入口 | 每话题折叠独有明细；CSV；后台隐藏审计API |
| 代码位置 | src/ds9_contribution_audit.py::record_round（33）；src/ds9_rejection_audit.py；web/frontend/src/sources/ds9-panel.ts |
| 调用关系 | 7天canonical去重并剔除其他来源重叠→每话题exact set delta；非抽奖最多100样本API/CSV仍保留 |
| 数据读写 | DB ds9审计；话题baseline集合；CSV |
| 当前状态 | 在用 |
| 已有测试 | tests/test_ds9_contribution_audit_v65.py；tests/test_ds9_topic_display_v106.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 UI-008；mockDS9与Following重叠、跨话题、未完整扫描、非抽奖样本。 |
| 风险/边界 | ‘独有’只代表7天已记录来源，不证明全网独有；隐藏不等于不存储。 |

### UI-009 任务日志浮窗与进度/性能

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | UI-009；初始圆形入口→展开蓝卡显示▼→卡片非交互区收起；logs/progress/performance更新保留用户开合状态 |
| 触发入口 | 原圆形任务日志入口；展开卡片三角/卡片点击 |
| 代码位置 | web/frontend/src/jobs/index.ts::setLogDockOpen（590）/updateJobUI（1363）；web/frontend/src/shell/nav.ts::bindNavigation（75） |
| 调用关系 | 初始圆形入口→展开蓝卡显示▼→卡片非交互区收起；logs/progress/performance更新保留用户开合状态 |
| 数据读写 | 前端state；Job净化日志/metrics |
| 当前状态 | 在用 |
| 已有测试 | web/frontend/src/jobs/log-dock.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 UI-009；离线打开/收起、日志正文点击、完成/失败/取消，窄窗与深浅主题。 |
| 风险/边界 | 与早期‘完成自动收起/▲’历史需求不同；以当前源码为准，需用户确认是否为预期。 |

### UI-010 Scheduler卡片与最近自动任务倒计时

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | UI-010；nearest ANY task→服务端时间校正→倒计时；到期只读REST确认；真正running才执行显示 |
| 触发入口 | 页面自动监控；auto status/SSE |
| 代码位置 | web/frontend/src/auto/index.ts；web/auto_scheduler.py::get_status（487） |
| 调用关系 | nearest ANY task→服务端时间校正→倒计时；到期只读REST确认；真正running才执行显示 |
| 数据读写 | 前端单计时器、Scheduler任务/phase/next_task |
| 当前状态 | 在用 |
| 已有测试 | web/frontend/src/auto/op-race.test.ts；web/frontend/src/auto/idle-countdown.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 UI-010；scan busy+participation due、stop/start乱序、风险恢复、到期。 |
| 风险/边界 | 1小时44分钟需具体基础槽位/当前时刻证据，不能只凭15分钟错峰期待判断Bug。 |

### UI-011 主题、侧边栏、页面导航与减少动画

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | UI-011；system/light/dark→localStorage；切换section调用所属loader；reduced-motion禁大动画 |
| 触发入口 | 主题按钮、sidebar折叠、导航 |
| 代码位置 | web/frontend/src/shell/theme.ts::initSystemPreferences（9）；web/frontend/src/shell/nav.ts；web/frontend/src/utils/motion.ts；web/frontend/src/styles/styles.css |
| 调用关系 | system/light/dark→localStorage；切换section调用所属loader；reduced-motion禁大动画 |
| 数据读写 | 浏览器localStorage与DOM；无业务任务 |
| 当前状态 | 在用 |
| 已有测试 | web/frontend/src/shell/theme.test.ts；web/frontend/src/utils/motion.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 UI-011；静态页面两主题、360px窗口、系统减少动态效果、快速导航。 |
| 风险/边界 | CSS保留project/quick-action历史规则，DOM缺失不代表当前功能。 |

### UI-012 通用确认、二维码焦点与净化toast

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | UI-012；二次确认promise+in-flight→Escape/焦点恢复；文本净化/结构错误优先 |
| 触发入口 | 清理/切Profile等确认；QR登录modal；错误提示 |
| 代码位置 | web/frontend/src/shell/confirm.ts::openAppConfirm（26）；web/frontend/src/shell/toast.ts；web/frontend/src/utils/text.ts |
| 调用关系 | 二次确认promise+in-flight→Escape/焦点恢复；文本净化/结构错误优先 |
| 数据读写 | 仅前端临时DOM；无自动业务重试 |
| 当前状态 | 在用 |
| 已有测试 | web/frontend/src/jobs/completion-responsiveness.test.ts；web/frontend/src/profiles/onboarding.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 UI-012；确认取消、escape、后台失败后另页成功、含HTML/Cookie错误文案。 |
| 风险/边界 | 净化不是完整安全边界，后端仍需身份/Origin校验。 |

### UI-013 账号资料与临时离线保留快照

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | UI-013；Cookie本地读取→nav有限缓存/single-flight→正式过期清登录；普通断网保留旧登录快照 |
| 触发入口 | 侧栏刷新、概览账号、登录完成 |
| 代码位置 | web/account_service.py::get_account_profile（313）；web/frontend/src/account/index.ts；web/frontend/src/account/account-preserve.ts |
| 调用关系 | Cookie本地读取→nav有限缓存/single-flight→正式过期清登录；普通断网保留旧登录快照 |
| 数据读写 | Profile元数据、账号缓存；Cookie本地，不向前端返回 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_account_service.py；web/frontend/src/account/login-state.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 UI-013；mock登录成功、网络失败、cookie真实-101过期、旧请求迟到。 |
| 风险/边界 | 资料刷新GET可能真实NAV请求，不能在本轮随意访问。 |

### UI-014 用户设置与LLM连接测试

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | UI-014；masked配置→实际provider测试总90秒→指纹ready→保存共享llm.env；普通参加fallback按UID设置 |
| 触发入口 | 概览设置刷新/测试/保存、API Key可见按钮 |
| 代码位置 | src/llm_settings.py::save_llm_settings（160）；src/llm_config.py；web/frontend/src/settings/index.ts |
| 调用关系 | masked配置→实际provider测试总90秒→指纹ready→保存共享llm.env；普通参加fallback按UID设置 |
| 数据读写 | shared llm.env；连接ready元数据；Profile用户配置 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_llm_settings.py；web/frontend/src/settings/llm-timeout.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 UI-014；mock慢于60秒成功、旧慢响应、新快响应、密钥mask、配置改变。 |
| 风险/边界 | LLM配置跨Profile共享；连接测试会真实LLM请求，人工需另授权。 |

### UI-015 开发诊断、日志筛选与包导出

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | UI-015；限定天数/类型/级别/query→脱敏日志→本地ZIP环境/Job/scheduler概要 |
| 触发入口 | 概览开发者工具；/api/diagnostics/logs、bundle |
| 代码位置 | src/diagnostics.py::build_diagnostics_bundle（66）；src/log_query.py；web/frontend/src/diagnostics/index.ts |
| 调用关系 | 限定天数/类型/级别/query→脱敏日志→本地ZIP环境/Job/scheduler概要 |
| 数据读写 | 日志与受控诊断包；不输出cookie/key |
| 当前状态 | 在用 |
| 已有测试 | tests/test_diagnostics_api.py；tests/test_log_query.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 UI-015；离线含secret fixture、非法日期/路径、bundle内容扫描。 |
| 风险/边界 | 诊断文件仍含账号昵称/UID等个人元数据，分享前复查。 |

### UI-016 本地运行环境检查与手动查更新

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | UI-016；本地配置health→版本/平台/数据根；手动GitHub Release读取→download链接，不安装 |
| 触发入口 | runtime信息；后台/api/updates/check；概览当前无常驻更新按钮 |
| 代码位置 | src/config_health.py::run_config_health_checks（57）；src/update_check.py；web/frontend/src/runtime/index.ts |
| 调用关系 | 本地配置health→版本/平台/数据根；手动GitHub Release读取→download链接，不安装 |
| 数据读写 | 本地report；更新JSON，不触B站 |
| 当前状态 | API在用；手动查更新页面按钮入口待核实 |
| 已有测试 | tests/test_runtime_api.py；tests/test_update_check.py；tests/test_updates_api.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 UI-016；mockGitHub最新/网络失败与无安装行为。 |
| 风险/边界 | production模块有实现，当前DOM删掉推广/更新控件；入口完整性待单独确认。 |

### UI-017 失败引导与用户显式重试入口

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | UI-017；结构化错误/risk优先分类 → 导航或用户显式startJob → 后端身份、风控、guard与Runner重新核验；不是后台自动重试 |
| 触发入口 | Job失败卡片/toast中的登录、LLM、再试一次、来源页与刷新列表按钮 |
| 代码位置 | web/frontend/src/jobs/index.ts::classifyJobFailure（304） / executeFailureAction（331） / renderFailureActions（359） |
| 调用关系 | 结构化错误/risk优先分类 → 导航或用户显式startJob → 后端身份、风控、guard与Runner重新核验；不是后台自动重试 |
| 数据读写 | 浏览器lastJobAttempt/action/params；后端新的Job及保护状态 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_error_domain_user_messages_v88.py；web/frontend/src/api/client.test.ts；web/frontend/src/jobs/participation-dedup.test.ts（已读断言；本轮未执行） |
| 风险/边界 | 前端复用旧params不能替代后端授权与当前身份；错误卡片不会自动释放UNKNOWN。 |
| 人工测试 | 见测试计划 UI-017；mock正式风险/网络/登录/LLM/UNKNOWN错误；点击各引导，捕获请求；连续点击及换Profile后检查参数。 |

## CLN 功能目录（10项）

### CLN-001 本人转发历史账本同步

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | CLN-001；verified NAV UID→本人feed/space分页→顶层owned FORWARD+可靠orig exact→每页upsert→本地guard reconciliation |
| 触发入口 | 清理中心同步并评估；sync_repost_history Job/API兼容入口 |
| 代码位置 | src/repost_cleanup.py::sync_repost_history（245）；src/repost_history.py |
| 调用关系 | verified NAV UID→本人feed/space分页→顶层owned FORWARD+可靠orig exact→每页upsert→本地guard reconciliation |
| 数据读写 | repost_history(uid,repost_dynamic_id,original_dynamic_id,身份、删除状态)；sync checkpoint |
| 当前状态 | 在用 |
| 已有测试 | tests/test_repost_cleanup_service.py；tests/test_repost_history.py；tests/test_v5_history_guard_reconciliation.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 CLN-001；mock本人/他人、普通原创、LLM普通抽奖、重复转发与已deleted tombstone。 |
| 风险/边界 | 同步是人工可触发B站只读，不是参与实时查重；当前没有后台周期同步。 |

### CLN-002 增量头部、历史续扫与dirty代际

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | CLN-002；新头部到旧边界；v12 frozen resume top+offset；success连续覆盖后checkpoint；dirty CAS不吃掉同步中新转发 |
| 触发入口 | 同步任务内部 |
| 代码位置 | src/repost_cleanup.py::sync_repost_history（245） / reconciliation_needs_sync（1675）；src/repost_history.py |
| 调用关系 | 新头部到旧边界；v12 frozen resume top+offset；success连续覆盖后checkpoint；dirty CAS不吃掉同步中新转发 |
| 数据读写 | repost_sync checkpoint/resume/dirty；history逐页提交 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_manual_cleanup_run_to_completion_v77.py；tests/test_v5_reconciliation.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 CLN-002；151页中断、恢复、同步途中confirmed新转发、checkpoint写失败。 |
| 风险/边界 | 不能只凭offset认为账号归属或完成；旧安装版本升级兼容需用户避免混跑。 |

### CLN-003 官方安全删除候选评估

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | CLN-003；original精确身份→公共分类/官方notice→明确开奖、完整中奖名单、本人未中、已过≥3天→safe |
| 触发入口 | 同步后评估；scan_expired_reposts人工Job；强制单original重评 |
| 代码位置 | src/repost_cleanup.py::_complete_winner_uids（554）/_assess_confirmed_lottery（761）/scan_expired_reposts（1202） |
| 调用关系 | original精确身份→公共分类/官方notice→明确开奖、完整中奖名单、本人未中、已过≥3天→safe |
| 数据读写 | assessment按UID+original；reason/checked/recheck/status |
| 当前状态 | 在用 |
| 已有测试 | tests/test_repost_cleanup_service.py；tests/test_repost_cleanup_candidates.py；tests/test_repost_cleanup_v79.py；tests/test_repost_cleanup_v80.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 CLN-003；官方未开奖、名单缺奖项、当前中奖、时间不可靠、刚过2天和3天边界。 |
| 风险/边界 | assessment不决定参与资格；不以cached旧safe直接删除，删除前再查。 |

### CLN-004 连续人工评估、breaker与重评

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | CLN-004；冻结当前backlog→有界120评估批次循环到自然结束→本地保存每original；临时失败15分钟再评、长期30天规则 |
| 触发入口 | 清理同步并评估按钮；单项重新评估 |
| 代码位置 | src/repost_cleanup.py::scan_expired_reposts（1202）；web/actions.py |
| 调用关系 | 冻结当前backlog→有界120评估批次循环到自然结束→本地保存每original；临时失败15分钟再评、长期30天规则 |
| 数据读写 | assessment/lifecycle、maintenance risk、Job进度 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_assessment_continuous_batches_m4.py；tests/test_manual_cleanup_run_to_completion_v77.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 CLN-004；250目标、连续网络失败、LLM熔断、期间新增history、取消。 |
| 风险/边界 | 120是内部批次非所有远端全局budget；新插入后续任务评估，避免永不结束。 |

### CLN-005 清理候选展示、搜索、选择与tombstone

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | CLN-005；本地history+assessment→safe/manual/deferred/blocked/deleted→关键词/时间排序→最多选择20 |
| 触发入口 | 清理中心候选；show_deleted本地读取 |
| 代码位置 | src/repost_cleanup.py::load_persisted_candidates（2033）/repost_cleanup_summary（2109）；web/frontend/src/repost-cleanup/index.ts |
| 调用关系 | 本地history+assessment→safe/manual/deferred/blocked/deleted→关键词/时间排序→最多选择20 |
| 数据读写 | selection前端；history/assessment读；deleted仅tombstone |
| 当前状态 | 在用 |
| 已有测试 | web/frontend/src/repost-cleanup/index.test.ts；tests/test_repost_cleanup_api.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 CLN-005；加载现有候选不scan；多级筛选、复制ID、20条封顶、未知checkbox不可选。 |
| 风险/边界 | 当前版本manual_review也允许额外确认人工删除，不再是严格V1只safe可删。 |

### CLN-006 暂缓与恢复清理候选

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | CLN-006；UID+repostID→本地lifecycle user defer；restore恢复评估展示，不发DELETE |
| 触发入口 | 候选行暂缓/恢复；本地API |
| 代码位置 | src/repost_cleanup.py::defer_candidate（2451）/restore_candidate（2456） |
| 调用关系 | UID+repostID→本地lifecycle user defer；restore恢复评估展示，不发DELETE |
| 数据读写 | cleanup lifecycle；recheck/deferred状态 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_repost_cleanup_v3_smart.py；tests/test_repost_cleanup_v3_wrapup.py；web/frontend/src/repost-cleanup/index.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 CLN-006；暂缓safe、恢复、30天重查、其他UID。 |
| 风险/边界 | ‘恢复’是候选显示/评估恢复，不是未知删除请求重试。 |

### CLN-007 勾选+二次确认删除本人转发

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | CLN-007；冻结runtime DB/UID→fresh本人顶层FORWARD+orig exact+官方/人工条件→claim delete_pending commit→POST operate/remove dyn_id_str=repostID retries0 |
| 触发入口 | 清理删除选中→POST /api/repost-cleanup/delete→Job delete_expired_reposts |
| 代码位置 | src/repost_cleanup.py::delete_reposts（2506）/_verify_owned_repost_detail（2470）；web/app.py::api_delete_expired_reposts（1139） |
| 调用关系 | 冻结runtime DB/UID→fresh本人顶层FORWARD+orig exact+官方/人工条件→claim delete_pending commit→POST operate/remove dyn_id_str=repostID retries0 |
| 数据读写 | delete_pending/deleted/delete_failed/unknown与requested_at/errors；tombstone永久 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_repost_cleanup_service.py；tests/test_repost_cleanup_candidates.py；tests/test_repost_delete_early_stop_v87.py；web/frontend/src/repost-cleanup/index.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 CLN-007；mock selected repostID≠original、归属错误、pending写失败、超时、业务拒绝、第一条formal。 |
| 风险/边界 | 真实删除不可逆；manual_review需额外风险确认；必须另授权真实测试。 |

### CLN-008 手动安全连续清理开关与执行

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | CLN-008；enable仅权限→人工Job冻结strict-safe集合→逐项fresh验证/删除；全局pending/unknown屏障停止 |
| 触发入口 | 允许手动安全清理开关+用户点击安全清理 |
| 代码位置 | src/repost_cleanup.py::auto_cleanup_delete（2880）/_strict_safe（2817）；web/frontend/src/repost-cleanup/index.ts |
| 调用关系 | enable仅权限→人工Job冻结strict-safe集合→逐项fresh验证/删除；全局pending/unknown屏障停止 |
| 数据读写 | Profile delete setting；history状态；Job累计结果 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_cleanup_scheduler_slim_v59.py；tests/test_auto_cleanup_m6.py；tests/test_delete_repost_lease_v9.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 CLN-008；开关开不点执行；模拟100safe、1unknown、用户取消、风险停止。 |
| 风险/边界 | 函数/action仍有auto历史名字但当前行为纯手动；不能当真实后台自动删。 |

### CLN-009 提前停止统计与可靠任务反馈

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | CLN-009；requested/processed/unprocessed/skipped/unknown分开→formal/cancel理由→重新读真实server状态 |
| 触发入口 | delete任务返回→Job终态→UI toast |
| 代码位置 | src/repost_cleanup.py::_cleanup_delete_result（3169）；web/actions.py；web/frontend/src/repost-cleanup/index.ts |
| 调用关系 | requested/processed/unprocessed/skipped/unknown分开→formal/cancel理由→重新读真实server状态 |
| 数据读写 | Job result；history/assessment；前端不伪造尾部unknown |
| 当前状态 | 在用 |
| 已有测试 | tests/test_repost_delete_early_stop_v87.py；tests/fixtures/v87_delete_risk_20.json；web/frontend/src/repost-cleanup/index.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 CLN-009；20条首条risk；5条后取消；网络unknown一条但尾部尚未发。 |
| 风险/边界 | HTTP Job失败与业务partial是两层，不能只看ok字段。 |

### CLN-010 清理健康、旧维护配置与内部cycle

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | CLN-010；读schema/cp/history/pending/unknown/风险→本地健康；旧maintenance设置兼容；cycle内部可同步→评估→delete |
| 触发入口 | health GET、本地recover接口；旧cleanup_maintenance_cycle无调度入口 |
| 代码位置 | src/repost_cleanup.py::cleanup_health_check（2188）/cleanup_maintenance_cycle（3229）；web/app.py::api_auto_cleanup_maintain_recover（1431） |
| 调用关系 | 读schema/cp/history/pending/unknown/风险→本地健康；旧maintenance设置兼容；cycle内部可同步→评估→delete |
| 数据读写 | 维护风险/diagnostic与本地设置 |
| 当前状态 | 在用兼容配置；内部cycle缺少现行页面/定时触发证据 |
| 已有测试 | tests/test_cleanup_scheduler_slim_v59.py；tests/test_scheduler_orchestration_m8.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 CLN-010；只读health保留guard；修改旧enabled后Scheduler仍无cleanup；测试cycle不从页面自动派发。 |
| 风险/边界 | 旧cycle仍有DELETE能力，只作为内部/遗留入口；不能因Scheduler移除就说源码没有自动顺序清理能力。 |

## NTF 功能目录（6项）

### NTF-001 通知渠道配置与事件开关

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | NTF-001；Server酱/钉钉/企微/飞书字段验证→敏感mask→Profile事件与渠道开关 |
| 触发入口 | 中奖通知页面；channels config/enabled、events API |
| 代码位置 | src/notification/service.py::save_channel_config（286）；web/frontend/src/notifications/index.ts |
| 调用关系 | Server酱/钉钉/企微/飞书字段验证→敏感mask→Profile事件与渠道开关 |
| 数据读写 | Profile notification config JSON；不返回密钥明文 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_domestic_notifications_m2.py；web/frontend/src/notifications/overview.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 NTF-001；离线保存渠道、空值保留/清除、独立事件开关、两Profile。 |
| 风险/边界 | 这些是外部通知HTTP，不是B站请求；配置保留个人信息。 |

### NTF-002 发送测试通知与外部派送

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | NTF-002；event+channel atomicclaim→HTTP provider payload→sent/failed/delivery_unknown；测试文案独立 |
| 触发入口 | 用户测试按钮；扫描candidate事件、开奖提醒等已确认caller |
| 代码位置 | src/notification/service.py::NotificationService.dispatch（686） / send_test |
| 调用关系 | event+channel atomicclaim→HTTP provider payload→sent/failed/delivery_unknown；测试文案独立 |
| 数据读写 | outbox及永久winner claim证据；发送结果 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_notification_outbox_recovery_v11.py；tests/test_winner_notification_copy_v54.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 NTF-002；mock四渠道超时、业务失败、HTTP后写失败、重复event。 |
| 风险/边界 | 外部HTTP可能真实发消息；人工需单独同意，默认mock。 |

### NTF-003 通知记录删除与隐藏tombstone

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | NTF-003；按event+channel删除展示记录/留抑制证据→只刷新本地面板 |
| 触发入口 | 通知记录行删除；local DELETE records/{event}/{channel} |
| 代码位置 | src/notification/service.py::delete_record（526）；web/app.py::api_notifications_delete_record（698） |
| 调用关系 | 按event+channel删除展示记录/留抑制证据→只刷新本地面板 |
| 数据读写 | outbox可裁剪；winner永久claims独立 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_domestic_notifications_m2.py；web/frontend/src/notifications/overview.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 NTF-003；本地删除已发送winner记录后重新扫描同线索。 |
| 风险/边界 | 用户看到记录消失不意味着历史发送证据被解除。 |

### NTF-004 Winner候选查看、跳转和抑制

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | NTF-004；候选过滤已抑制/明确自动回复→sender+summary+安全URL→手工抑制稳定key |
| 触发入口 | 中奖线索表；suppress POST / DELETE兼容 |
| 代码位置 | src/winner_scan/service.py::list_candidates（563）/suppress_candidate（535）；web/frontend/src/notifications/index.ts |
| 调用关系 | 候选过滤已抑制/明确自动回复→sender+summary+安全URL→手工抑制稳定key |
| 数据读写 | winner state suppressed keys；候选本体保留 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_winner_deep_scan_m3.py；web/frontend/src/notifications/overview.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 NTF-004；正常私信链接、危险scheme、无链接、删除一条后重扫。 |
| 风险/边界 | 标题中奖提醒是通知文案，候选事实仍疑似；昵称空可回退UID。 |

### NTF-005 账号消息统计与@未读确认

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | NTF-005；B站未读计数→本地基线升高提醒→用户已看仅本地ack；降低时校正 |
| 触发入口 | 账号extras；ack-at-unread按钮/API |
| 代码位置 | src/message_api.py；src/message_watch.py::evaluate_at_unread_alert（73） / acknowledge_at_unread；web/account_service.py |
| 调用关系 | B站未读计数→本地基线升高提醒→用户已看仅本地ack；降低时校正 |
| 数据读写 | MessageWatchRow UID基线；账号extras缓存 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_message_watch.py；tests/test_message_api.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 NTF-005；mock初始未读、升高、降低、人工ack。 |
| 风险/边界 | 当前DOM是否保留每种历史提醒交互按入口附录，不推断都可见。 |

### NTF-006 预留通知事件及尚未接入的生产者

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | NTF-006；winning_confirmed/platform_risk/auto_task_error/auto_task_completed可在通用模型中表示；生产调用核实只有Winner winning_candidate与渠道测试 |
| 触发入口 | 通知事件配置API/UI；枚举本身不产生事件 |
| 代码位置 | src/notification/service.py::EVENT_IDS（51）；web/frontend/src/notifications/index.ts::EVENT_PRODUCER_PENDING（47） |
| 调用关系 | winning_confirmed/platform_risk/auto_task_error/auto_task_completed可在通用模型中表示；生产调用核实只有Winner winning_candidate与渠道测试 |
| 数据读写 | Profile notify_config事件开关；没有上述四事件的已确认生产链 |
| 当前状态 | 预留/未接入；生产者未确认 |
| 已有测试 | tests/test_domestic_notifications_m2.py（通用事件模型断言；不是生产者E2E；本轮未执行） |
| 风险/边界 | 三种risk/task事件在UI禁用并标未接入；winning_confirmed可配置但未找到真实确认生产者；不能宣传已支持中奖确认或自动任务风险推送。 |
| 人工测试 | 见测试计划 NTF-006；离线读取事件配置/渲染；保存合法开关但不dispatch；检查三种未接入事件disabled标记。 |

## SYS 功能目录（27项）

### SYS-001 数据根定位与冻结runtime Profile

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-001；选根/env/locator→旧布局迁移→选runtime一次冻结→DB/Cookie/output/cache/log路径只按冻结Profile |
| 触发入口 | source/launcher --profile；BINGGO_DATA_ROOT等环境；首次选择数据根 |
| 代码位置 | src/data_paths.py::get_runtime_profile_id（368）；src/app_paths.py；src/dashboard_server.py |
| 调用关系 | 选根/env/locator→旧布局迁移→选runtime一次冻结→DB/Cookie/output/cache/log路径只按冻结Profile |
| 数据读写 | root profiles/account-N；shared；active_profile.json/locator |
| 当前状态 | 在用 |
| 已有测试 | tests/test_data_paths.py；tests/test_runtime_instances_v109.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-001；临时rootA/B和两个Profile；启动后修改active pointer。 |
| 风险/边界 | 读取路径可能初始化目录/迁移，审计只静态读，不调用模块初始化。 |

### SYS-002 Profile列表、metadata与手工创建

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-002；registry锁+writer/runtime idle→account号分配→metadata→运行状态装配 |
| 触发入口 | /api/profiles及兼容POST create；新号QR主UI |
| 代码位置 | src/profile_manager.py；web/app.py::api_profiles（341）/api_create_profile（418） |
| 调用关系 | registry锁+writer/runtime idle→account号分配→metadata→运行状态装配 |
| 数据读写 | Profile目录/profile.json、active pointer、runtime registry |
| 当前状态 | 在用 |
| 已有测试 | tests/test_profile_manager.py；tests/test_profile_api.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-002；临时创建两个Profile、重复MID、元数据冲突。 |
| 风险/边界 | 空Profile创建兼容API仍在，正式UI优先扫码创建。 |

### SYS-003 扫码登录新账号与幂等完成

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-003；二维码临时会话→手机确认→验证CookieUID/MID一致→existing UID复用或staged新目录→receipt幂等 |
| 触发入口 | Profile扫码登录新账号；onboarding start/poll/complete |
| 代码位置 | src/login_onboarding.py；web/frontend/src/profiles/index.ts；web/app.py::api_onboarding_complete（868） |
| 调用关系 | 二维码临时会话→手机确认→验证CookieUID/MID一致→existing UID复用或staged新目录→receipt幂等 |
| 数据读写 | 受限secret sidecars、Profile数据、receipt；TTL清理 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_login_onboarding_m7.py；web/frontend/src/profiles/onboarding.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-003；mockwaiting/scanned/success、关闭modal、重复complete、UID冲突、运行中同UID。 |
| 风险/边界 | frozen新Profile DB初始化使用sys.executable -c存在静态路径风险，需安装版验证，见D-002。 |

### SYS-004 当前账号扫码重登录与登出

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-004；生成QR/轮询→核对当前Profile UID→原子写Cookie→刷新账户；logout只清当前Cookie |
| 触发入口 | 原登录Job；sidebar logout；QRCode GET |
| 代码位置 | src/bilibili_login.py::login_with_qrcode（475）；src/bilibili_auth.py；web/account_service.py |
| 调用关系 | 生成QR/轮询→核对当前Profile UID→原子写Cookie→刷新账户；logout只清当前Cookie |
| 数据读写 | 当前Profile cookies.txt、metadata、账号cache |
| 当前状态 | 在用 |
| 已有测试 | tests/test_bilibili_login_status.py；tests/test_bilibili_login_stability.py；tests/test_login_identity_guard_v8.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-004；mock同UID成功、异UID拒绝、cancel、不让迟到成功恢复已登出视图。 |
| 风险/边界 | logout修改Cookie，用户真实数据禁止在审计执行。 |

### SYS-005 Profile切换与重启等待

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-005；确认→Runner/Scheduler idle+restart gate→active写→75重启→轮询runtime==active==target→reload |
| 触发入口 | Profile切换/重启生效按钮 |
| 代码位置 | web/app.py::api_switch_profile（457）；web/frontend/src/profiles/index.ts::waitForProfileRestart（58） |
| 调用关系 | 确认→Runner/Scheduler idle+restart gate→active写→75重启→轮询runtime==active==target→reload |
| 数据读写 | active pointer；旧runtime固定；独立CookieDB |
| 当前状态 | 在用 |
| 已有测试 | tests/test_profile_api.py；tests/test_restart_control.py；web/frontend/src/profiles/index.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-005；mock旧服务断线/新服务不同身份/timeout、重复点击。 |
| 风险/边界 | 切换与新窗口运行是两个功能；禁止SQLite/Cookie运行时重绑定。 |

### SYS-006 删除未运行Profile与中断恢复

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-006；排除current/selected/runtime/service锁→staged deleting目录→registry更新→受控回收/恢复 |
| 触发入口 | Profile删除二次确认；DELETE profile |
| 代码位置 | src/profile_manager.py::delete_profile（204）；web/app.py::api_delete_profile（531） |
| 调用关系 | 排除current/selected/runtime/service锁→staged deleting目录→registry更新→受控回收/恢复 |
| 数据读写 | 仅指定Profile目录与registry；不删共享LLM |
| 当前状态 | 在用 |
| 已有测试 | tests/test_profile_manager.py；tests/test_runtime_instances_v109.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-006；临时Profile运行锁、active指针、rename中断、另一个Profile。 |
| 风险/边界 | 是本地删除含Cookie/DB，真实人工需备份且另确认。 |

### SYS-007 已有Profile新窗口独立运行

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-007；冻结profile/port/instanceID args→启动真实launcher/supervisor→ready身份吻合→打开独立浏览器；ready已有仅open |
| 触发入口 | Profile新窗口运行/打开窗口；launch API |
| 代码位置 | src/runtime_launch.py；web/app.py::api_launch_profile（392）；web/frontend/src/profiles/index.ts |
| 调用关系 | 冻结profile/port/instanceID args→启动真实launcher/supervisor→ready身份吻合→打开独立浏览器；ready已有仅open |
| 数据读写 | shared runtime registry；每Profile租约；端口base..base+19 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_runtime_instances_v109.py；web/frontend/src/profiles/index.test.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-007；fake两个Profile、端口占用、ready身份错、重复launch。 |
| 风险/边界 | 并行账号会增加总请求压力，错峰非全局限速。 |

### SYS-008 runtime registry、PID身份与双生命周期锁

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-008；registry原子锁→starting保留期限→PID/OS runtime与service锁+exact ready probe→仅本instance清理 |
| 触发入口 | launcher reserve/claim/ready/clear；Profile状态列表 |
| 代码位置 | src/runtime_instances.py::get_runtime_instance（241） |
| 调用关系 | registry原子锁→starting保留期限→PID/OS runtime与service锁+exact ready probe→仅本instance清理 |
| 数据读写 | shared/runtime-registry.json；Profile runtime/service OS锁 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_runtime_instances_v109.py；tests/test_profile_lock_v9.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-008；stale registry、PID复用、orphan service锁、双Profile独立退出。 |
| 风险/边界 | 列表GET会清陈旧registry；本轮未调用，不改真实registry。 |

### SYS-009 源码supervisor与安装版launcher

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-009；父runtime lease→--serve子process+stdinPIPE→ready→browser/tray→wait健康loop；75才restart |
| 触发入口 | python scripts/run_dashboard.py；Binggo.exe |
| 代码位置 | scripts/run_dashboard.py::main（14）；binggo_launcher.py::_supervise_server（444） / main |
| 调用关系 | 父runtime lease→--serve子process+stdinPIPE→ready→browser/tray→wait健康loop；75才restart |
| 数据读写 | launcher日志；registry；runtime/service锁；冻结args |
| 当前状态 | 在用 |
| 已有测试 | tests/test_binggo_launcher.py；tests/test_dashboard_server.py；tests/test_tray_runtime_lifecycle_v112a.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-009；fake普通运行不return、0/9退出、75新PID、Ctrl+C，source/frozen args。 |
| 风险/边界 | 直接--serve不是完整托盘supervisor体验；不同版本launcher共用数据根不要混跑。 |

### SYS-010 Windows托盘创建与独立菜单

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-010；pystray Windows backend+Pillow正式图标→setup先visibleTrue再ready→打开控制台/退出当前Profile菜单 |
| 触发入口 | ready后supervisor启动WindowsTray |
| 代码位置 | src/windows_tray.py::WindowsTray._run（138） / start；binggo_launcher.py::_supervise_server（444） |
| 调用关系 | pystray Windows backend+Pillow正式图标→setup先visibleTrue再ready→打开控制台/退出当前Profile菜单 |
| 数据读写 | 每实例独立Icon/thread/title；无浏览器生命周期绑定 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_system_tray_v110.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-010；mockvisible设置失败与True、菜单A/B、浏览器关闭、Explorer隐藏。 |
| 风险/边界 | visible属性不等于OS用户实际看到图标，^隐藏区/Explorer外部状态不能绝对检测。 |

### SYS-011 托盘恢复与异常安全退出

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-011；start false或message-loop死亡→有限恢复3次→失败报错+shutdown gate→等待任务/服务退出→移除图标 |
| 触发入口 | supervisor周期检查真实thread健康 |
| 代码位置 | binggo_launcher.py::_start_supervised_tray（376） / _supervise_server；src/windows_tray.py::is_healthy（158） |
| 调用关系 | start false或message-loop死亡→有限恢复3次→失败报错+shutdown gate→等待任务/服务退出→移除图标 |
| 数据读写 | 受管线程与进程；锁/registry按对应实例清理 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_tray_runtime_lifecycle_v112a.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-011；startFalse、线程death、恢复成功/失败、运行Job、已有75切换。 |
| 风险/边界 | 初始化尚未ready的子进程清理与运行中安全shutdown不同，不能混称全程绝不terminate。 |

### SYS-012 托盘健康tooltip

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-012；port+profile+instance exact→缓存scheduler/job/risk→昵称、正常/扫描/停止/静默/风险/不可用、next scan→UTF16安全127单位 |
| 触发入口 | supervisor约60秒本机tray-health GET |
| 代码位置 | src/windows_tray.py::health_tooltip（47） / update_title；web/tray_health.py::current_tray_health（33）；binggo_launcher.py::_read_tray_health（401） |
| 调用关系 | port+profile+instance exact→缓存scheduler/job/risk→昵称、正常/扫描/停止/静默/风险/不可用、next scan→UTF16安全127单位 |
| 数据读写 | 只读当前runtime快照；上次成功仅当前进程内存 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_tray_health_tooltip_v113.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-012；fake两个profile、auto参与最近但扫描稍后、winner未偏移、emoji长昵称、HTTP失败。 |
| 风险/边界 | 重启后上次成功可未知；下次扫描包含Winner原计划，不包含自动参与。 |

### SYS-013 stdin安全退出与supervisor孤儿防护

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-013；new_work gate关闭→stop Scheduler→cancel当前Job→等terminal与调度线程→uvicorn exit→parent wait→清registry/锁→tray stop |
| 触发入口 | 托盘退出/Ctrl+C；子进程pipe EOF |
| 代码位置 | src/tray_shutdown.py::shutdown_current_service（37）；src/restart_control.py；binggo_launcher.py::_supervise_server（444） |
| 调用关系 | new_work gate关闭→stop Scheduler→cancel当前Job→等terminal与调度线程→uvicorn exit→parent wait→清registry/锁→tray stop |
| 数据读写 | 现有checkpoint/unknown不清；退出通道每实例独立 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_tray_runtime_lifecycle_v112a.py；tests/test_system_tray_v110.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-013；fake忙Job安全等待、parent EOF、A/B pipe。 |
| 风险/边界 | 旧安装版无新EOF契约不能被新源码改造，残留实例需先核实再由用户处理。 |

### SYS-014 SQLite Schema12事务迁移与校验

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-014；旧版本有序迁移→结构先校验/再索引→meta只成功后更新→future version拒绝→rollback可重跑 |
| 触发入口 | DB首次init/升级 |
| 代码位置 | src/db/schema.py::init_db（691）；src/db/models.py；src/db/engine.py |
| 调用关系 | 旧版本有序迁移→结构先校验/再索引→meta只成功后更新→future version拒绝→rollback可重跑 |
| 数据读写 | Profile binggo.db；活动/guard/history/assessment/notifications相关状态表 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_sqlite_data_layer.py；tests/test_participation_guard_schema.py；tests/test_repost_history_schema.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-014；v1/v2/v3到12、畸形表、迁移中断、重复init、高版本13。 |
| 风险/边界 | 新DB被旧程序读写防重/新增schema不认识，升级后旧版不能执行参与/清理。 |

### SYS-015 DB codec、session、snapshot与兼容逻辑路径

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-015；类型/JSON安全codec→短session commit/rollback→逻辑旧文件名映射DB行→snapshot读写 |
| 触发入口 | store模块；JSON导入/seed adapter |
| 代码位置 | src/db/activity_codec.py::activity_dict_to_row（40）；src/db/json_cols.py；src/db/session.py；src/db/snapshots.py；src/user_data.py |
| 调用关系 | 类型/JSON安全codec→短session commit/rollback→逻辑旧文件名映射DB行→snapshot读写 |
| 数据读写 | DB真实来源；旧data/output JSON路径通常仅兼容标识 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_sqlite_data_layer.py；tests/test_pipeline_persisted_state.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-015；roundtrip business_type/prizes/旧JSON、事务失败、无真实旧文件。 |
| 风险/边界 | 不同session提交之间非全流程单事务，复合操作要看顺序。 |

### SYS-016 JSON一次性导入与活动/state种子

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-016；writer lease+input验证→dry-run/nonempty拒绝→显式force→DB commit→hash归档→移除已归档源 |
| 触发入口 | scripts/import_json_to_db.py；首次bootstrap；seed导出工具 |
| 代码位置 | src/db/import_json.py::run_import（892）；src/activity_seed.py；src/state_seed.py |
| 调用关系 | writer lease+input验证→dry-run/nonempty拒绝→显式force→DB commit→hash归档→移除已归档源 |
| 数据读写 | Profile DB；输入JSON备份/归档；可选用户seed |
| 当前状态 | 在用 |
| 已有测试 | tests/test_sqlite_data_layer.py；tests/test_activity_seed.py；tests/test_state_seed.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-016；临时非空库、force明确确认、缺输入、archive失败、source cp白名单。 |
| 风险/边界 | 归档是commit后阶段，DB已提交但归档失败可能需人工检查，不声称全链原子。 |

### SYS-017 旧数据根迁移、备份与权限

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-017；定位旧root→SQLite backup+quick_check→staged profile→原子迁入/指针；保护密钥权限 |
| 触发入口 | 首次配置/旧布局发现 |
| 代码位置 | src/data_paths.py::initialize_data_layout（590）；src/app_paths.py；src/secure_files.py |
| 调用关系 | 定位旧root→SQLite backup+quick_check→staged profile→原子迁入/指针；保护密钥权限 |
| 数据读写 | 旧数据备份、Profile新布局、locator、Cookie/shared LLM |
| 当前状态 | 在用 |
| 已有测试 | tests/test_data_paths.py；tests/test_secure_files.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-017；临时legacy数据库、已有目标、损坏库、不同根、Windows权限失败。 |
| 风险/边界 | 文件权限为best effort，Windows ACL实际效果需要系统人工验收。 |

### SYS-018 Profile writer lease与本地原子文件锁

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-018；Profile writer OS lease+进程reentrance→窄atomic path lock→temp fsync+replace→释放 |
| 触发入口 | Web写Job/CLI写脚本；state guard I/O |
| 代码位置 | src/profile_writer_lease.py::profile_writer_lease（175）；src/user_data_lock.py；src/user_data_io.py |
| 调用关系 | Profile writer OS lease+进程reentrance→窄atomic path lock→temp fsync+replace→释放 |
| 数据读写 | 各Profile lockfiles；JSON sidecars；真实OS持有状态 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_profile_writer_lease_m5.py；tests/test_profile_state_atomicity_v10.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-018；Web/CLI竞争、不同profile、replace PermissionError有限重试、崩溃。 |
| 风险/边界 | 旧工具某些路径不走WriterLease，报告OPS入口注明边界。 |

### SYS-019 活动坏链、充电排除与失败ledger

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-019；可靠失效码→tombstone/aliases；充电/不管理类型排除；普通失败ledger诊断 |
| 触发入口 | scan/classify/enrich；本地维护/手工忽略 |
| 代码位置 | src/dead_links.py::partition_alive_dynamic_ids（66）；src/invalid_activities.py；src/activity_failures.py；src/lottery_classifier.py |
| 调用关系 | 可靠失效码→tombstone/aliases；充电/不管理类型排除；普通失败ledger诊断 |
| 数据读写 | 本地坏链/活动失败DB；manual ignore独立 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_classify_deleted_link.py；tests/test_lottery_classifier_upower.py；tests/test_activity_failures_v60.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-019；失效4101152、一般HTTP500/风险、充电、unknown正文。 |
| 风险/边界 | 失败记录不是已参与状态，删除失败ledger不会解除guard。 |

### SYS-020 生产日志、脱敏、保留期与性能跨度

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-020；context Profile/job/stage→JSONL按日→缓冲flush→每日低频retention7天→metrics摘要 |
| 触发入口 | 服务/Job/CLI init_logging；diagnostics |
| 代码位置 | src/app_logging.py::DailyFileHandler（144）；src/log_redact.py；src/log_context.py；src/log_span.py；src/observability.py |
| 调用关系 | context Profile/job/stage→JSONL按日→缓冲flush→每日低频retention7天→metrics摘要 |
| 数据读写 | Profile logs；Job有界净化日志；query context脱敏 |
| 当前状态 | 在用 |
| 已有测试 | tests/test_app_logging.py；tests/test_log_retention_frequency_v92.py；tests/test_observability_consistency_v15.py；tests/test_scan_observability.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-020；fake日期、保留清理失败、flush即时、secret字段/URLquery、Job metrics。 |
| 风险/边界 | 日志会裁剪，不能当长期mutation屏障；生产trace可能敏感需bundle再净化。 |

### SYS-021 本机API边界、契约与schema

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-021；loopback Host/Origin→参数schema/动作白名单→本地身份→结构化error→contract header；launch额外same-origin |
| 触发入口 | 所有FastAPI routes及response模型 |
| 代码位置 | web/security.py::LocalControlPlaneGuard（110）；web/api_contract.py；web/app.py；web/schemas/jobs.py |
| 调用关系 | loopback Host/Origin→参数schema/动作白名单→本地身份→结构化error→contract header；launch额外same-origin |
| 数据读写 | request无凭据回显；runtime/Job/诊断DTO |
| 当前状态 | 在用 |
| 已有测试 | tests/test_local_control_plane_security_v8.py；tests/test_web_api.py；tests/test_api_errors.py；tests/test_runtime_api.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-021；foreignHost/Origin、另端口launch、非法ID/action、legacy/app.js 410。 |
| 风险/边界 | 普通写Origin允许loopback并非统一same-port；无密码认证主要依赖本机边界。 |

### SYS-022 离线测试设施与受控E2E服务

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-022；测试收集前temp root→socket/DNS仅loopback+禁代理→fake业务；E2E仅专用临时root种子+条件hook |
| 触发入口 | pytest conftest；scripts/run_e2e_server.py；前端test/e2e |
| 代码位置 | web/e2e_hooks.py::install_e2e_hooks（120）；tests/conftest.py；tests/offline_guard.py；web/e2e_seed.py；web/frontend/playwright.config.ts |
| 调用关系 | 测试收集前temp root→socket/DNS仅loopback+禁代理→fake业务；E2E仅专用临时root种子+条件hook |
| 数据读写 | 临时Profile/DB；test hooks仅BINGGO_E2E；browser artifacts |
| 当前状态 | 在用开发基础设施 |
| 已有测试 | tests/test_offline_isolation_v6.py；tests/test_run_e2e_server.py；web/frontend/e2e/smoke.spec.ts（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-022；静态核对guard+mock；另行测试仅用临时root，勿连接真实8787。 |
| 风险/边界 | Playwright旧selector与reuseExistingServer需先校正/核对，不应直接接真实实例。 |

### SYS-023 正式Windows打包与安装/卸载

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-023；正式launcher+collect src/web+pystray._win32+Pillow+图标+frontend dist→PyInstaller→portableZIP→InnoSetup |
| 触发入口 | packaging/windows/build.ps1；GitHub Windows release workflow |
| 代码位置 | packaging/windows/installer.iss::KillBinggoProcesses（55）；packaging/windows/binggo.spec |
| 调用关系 | 正式launcher+collect src/web+pystray._win32+Pillow+图标+frontend dist→PyInstaller→portableZIP→InnoSetup |
| 数据读写 | 受控build/dist；Setup、portable；安装目录不含真实Profile |
| 当前状态 | 在用 |
| 已有测试 | tests/test_system_tray_v110.py::test_packaging_includes_icon_and_tray_backend；其余需静态/人工（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-023；只审脚本与本次产物manifest；安装验收由用户安全退出旧实例后另做。 |
| 风险/边界 | installer按名taskkill /F /T全Binggo旧实例；native build exit检查不全；详见D-001/D-005。 |

### SYS-024 macOS打包与跨平台适配

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-024；Python build→正式launcher .app+ICNS→DMG/ZIP、符号链接保留；非Windows tray不创建 |
| 触发入口 | packaging/macos/build.py；release-macos workflow |
| 代码位置 | packaging/macos/build.py::main（21）；packaging/macos/binggo.spec；packaging/icon_raster.py |
| 调用关系 | Python build→正式launcher .app+ICNS→DMG/ZIP、符号链接保留；非Windows tray不创建 |
| 数据读写 | macOS受控产物；平台目录根 |
| 当前状态 | 在用发布入口，平台验收待核实 |
| 已有测试 | tests/test_app_paths.py；专用macOS人工验收未执行（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-024；静态核对console/windowed资源、arm64输出、zip symlink；不在Windows实际安装。 |
| 风险/边界 | 当前环境无法验证macOS原生窗口/签名/安装生命周期。 |

### SYS-025 官网静态展示与导航

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-025；语言/主题系统识别→滚动导航/移动菜单→hero/tour截图切换→FAQ→GitHub下载/反馈 |
| 触发入口 | docs/index.html GitHub Pages |
| 代码位置 | docs/landing.js:1；docs/landing.css；docs/index.html |
| 调用关系 | 语言/主题系统识别→滚动导航/移动菜单→hero/tour截图切换→FAQ→GitHub下载/反馈 |
| 数据读写 | 浏览器DOM/localStorage；静态图片manifest |
| 当前状态 | 在用官网，不是本地程序功能 |
| 已有测试 | 无专属自动化行为测试；可离线页面验收（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-025；本地静态打开，操作菜单/FAQ/展示卡、键盘/减少动画，勿点网络下载。 |
| 风险/边界 | 宣传功能与当前源码需分开；SVG仅静态资产无业务入口。 |

### SYS-026 B站HTTP协议、Cookie与WBI签名

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-026；本地Cookie→受控session请求→WBI签名/限速/预算→结构化网络/HTTP/API错误 |
| 触发入口 | 所有BilibiliClient调用；登录/扫描/动作 |
| 代码位置 | src/bilibili_client.py::wbi_sign（92） / BilibiliClient；src/bilibili_auth.py |
| 调用关系 | 本地Cookie→受控session请求→WBI签名/限速/预算→结构化网络/HTTP/API错误 |
| 数据读写 | Cookie仅当前Profile；WBI缓存与session；安全日志 |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_bilibili_rate_limit.py；tests/test_bilibili_login_cookies.py；tests/test_error_domain_user_messages_v88.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-026；mock HTTP与WBI key、断网、429、-352、-412、畸形JSON；不加载真实Cookie。 |
| 风险/边界 | 请求语义有差异：只读默认可有限重试；非幂等动作显式retries=0；-315/-412不是正式风险。 |

### SYS-027 LLM抽奖判读、结构提取与分类缓存

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | SYS-027；正文与类型证据→LLM判抽奖/解析奖品条件日期→本地分类/解析缓存→结构校验；连续失败breaker停止 |
| 触发入口 | 普通转发抽奖classify/enrich；清理手动评估；设置连接测试 |
| 代码位置 | src/forward_parser.py::classify_forward_lottery（639） / parse_forward_content；src/llm_client.py；src/forward_classify_cache.py；src/forward_parse_cache.py；src/llm_resilience.py |
| 调用关系 | 正文与类型证据→LLM判抽奖/解析奖品条件日期→本地分类/解析缓存→结构校验；连续失败breaker停止 |
| 数据读写 | Profile SQLite forward_classify_cache / forward_parse_cache表、content hash及解析版本；shared/llm.env。CACHE_PATH 的JSON名是兼容常量，不是当前缓存落盘方式。 |
| 当前状态 | 在用（生产分类/补齐/手动评估与显式设置测试） |
| 已有测试 | tests/test_classify_forward_lottery.py；tests/test_llm_client.py；tests/test_llm_resilience_v21.py；tests/test_forward_parser_extract.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 SYS-027；mock LLM真/假/损坏结构/90秒超时/连续503，确认缓存和breaker；禁止真实LLM。 |
| 风险/边界 | LLM不是官方中奖证明；内容缓存不等于永久非抽奖记录；配置未ready不得把失败当非抽奖。 |

## OPS 功能目录（24项）

### OPS-001 DS1～7 独立检查 CLI

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-001；writer lease → check_update → save_result → commit_source_checkpoint；仅提取/保存来源链接，不执行完整分类Pipeline |
| 触发入口 | python scripts/check_ds1.py～check_ds7.py [--force] |
| 代码位置 | src/check_cli.py::run_check（12）；scripts/check_ds1.py；scripts/check_ds2.py；scripts/check_ds3.py；scripts/check_ds4.py；scripts/check_ds5.py；scripts/check_ds6.py；scripts/check_ds7.py |
| 调用关系 | writer lease → check_update → save_result → commit_source_checkpoint；仅提取/保存来源链接，不执行完整分类Pipeline |
| 数据读写 | 来源快照与checkpoint |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_ds_checkpoint_commit.py；tests/test_profile_writer_lease_m5.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-001；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | CLI检查成功不等于抽奖已入库；--force会重复读取远端。 |

### OPS-002 命令行参与

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-002；writer lease → 当前库读类型 → participate_activity(preflight,persist) → stdout与退出码 |
| 触发入口 | python scripts/participate.py <dynamic_id> |
| 代码位置 | scripts/participate.py::main（39） |
| 调用关系 | writer lease → 当前库读类型 → participate_activity(preflight,persist) → stdout与退出码 |
| 数据读写 | participations、各guard、审计 |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_participation_dedup_entrypoints.py；tests/test_comment_guard_entrypoints_v81.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-002；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | 会产生真实参与POST；默认没有Web人工UNKNOWN授权。 |

### OPS-003 消息命令行检查

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-003；unread/at/reply/私信列表、单会话消息 → JSON/文本；复用message_api |
| 触发入口 | python scripts/check_messages.py（参数见build_parser） |
| 代码位置 | scripts/check_messages.py::build_parser（98） / main |
| 调用关系 | unread/at/reply/私信列表、单会话消息 → JSON/文本；复用message_api |
| 数据读写 | 无参与状态写入 |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_message_api.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-003；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | B站只读网络仍消耗请求；不标记已读。 |

### OPS-004 旧分类输出详情补齐CLI

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-004；writer lease → classify输出待补条目 → 并发详情 → save_enriched |
| 触发入口 | python scripts/fetch_activity_info.py |
| 代码位置 | scripts/fetch_activity_info.py::main（16）；src/fetch_activity_info.py |
| 调用关系 | writer lease → classify输出待补条目 → 并发详情 → save_enriched |
| 数据读写 | activities及enriched快照 |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_missing_enrich.py；tests/test_fetch_enrich_pending.py；tests/test_fetch_path_effectiveness.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-004；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | 与公共Pipeline详情阶段重叠，不是新发现来源。 |

### OPS-005 本地活动治理CLI

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-005；过滤充电/坏链 → 可选并发补齐详情 → 保存保留活动与统计 |
| 触发入口 | python scripts/maintain_local_activities.py |
| 代码位置 | scripts/maintain_local_activities.py::main（53） |
| 调用关系 | 过滤充电/坏链 → 可选并发补齐详情 → 保存保留活动与统计 |
| 数据读写 | 活动库/维护输出；--dry-run不写回但补详情仍可联网 |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_profile_writer_lease_m5.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-005；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | --dry-run不是0网络；worker广义异常会记失败后继续，正式风险边界需单独验证。 |

### OPS-006 缺失详情补录CLI

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-006；筛出缺详情 → enrich逐条 → 追加可靠成功；失败集合输出 |
| 触发入口 | python scripts/backfill_missing_details.py |
| 代码位置 | scripts/backfill_missing_details.py::main（26） |
| 调用关系 | 筛出缺详情 → enrich逐条 → 追加可靠成功；失败集合输出 |
| 数据读写 | activities；ROOT/data/output/backfill_missing_failures.json |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_fetch_enrich_pending.py；tests/test_missing_enrich.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-006；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | 失败文件使用仓库旧路径；异常catch后继续，不等价当前主Job全局风控。 |

### OPS-007 全量重建与失败重建CLI

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-007；本地全部可补活动或旧失败集合 → enrich → 回写/失败输出 |
| 触发入口 | rebuild_all_activity_details.py；retry_failed_rebuild.py |
| 代码位置 | scripts/rebuild_all_activity_details.py::main（42）；scripts/retry_failed_rebuild.py::main（31） |
| 调用关系 | 本地全部可补活动或旧失败集合 → enrich → 回写/失败输出 |
| 数据读写 | 活动快照与仓库data/output失败文件 |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_profile_writer_lease_m5.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-007；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | 广义异常继续；不应作为在线运行实例的无风险修复工具。 |

### OPS-008 批量排除失效活动CLI

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-008；writer lease → 收集本地ID → 远程detail → 仅确证失效purge → 本地状态刷新 |
| 触发入口 | python scripts/purge_all_dead_links.py |
| 代码位置 | scripts/purge_all_dead_links.py::main（19）；src/dead_links.py |
| 调用关系 | writer lease → 收集本地ID → 远程detail → 仅确证失效purge → 本地状态刷新 |
| 数据读写 | 活动/来源本地记录；不是删除本人B站动态 |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_dead_links_api.py；tests/test_classify_deleted_link.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-008；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | 网络失败不能等同404；真实本地删除需备份。 |

### OPS-009 充电抽奖审查及修正CLI

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-009；读取存储 → 标记charging skipped → 写回 → 刷新列表缓存并核验 |
| 触发入口 | python scripts/audit_charging_activities.py |
| 代码位置 | scripts/audit_charging_activities.py::main（18） |
| 调用关系 | 读取存储 → 标记charging skipped → 写回 → 刷新列表缓存并核验 |
| 数据读写 | 本地enriched/activities |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_charging_lottery.py；tests/test_lottery_classifier_upower.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-009；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | 名称含审查但会写库，不是只读命令。 |

### OPS-010 旧开奖时间迁移与缺失时间补全

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-010；已有时间标准化/可靠日期优先；缺失时间按参与/入库基准推断 → 本地状态刷新 |
| 触发入口 | migrate_lottery_times.py；backfill_missing_lottery_times.py |
| 代码位置 | scripts/migrate_lottery_times.py::main（28）；scripts/backfill_missing_lottery_times.py::main（17） |
| 调用关系 | 已有时间标准化/可靠日期优先；缺失时间按参与/入库基准推断 → 本地状态刷新 |
| 数据读写 | 本地条件、lottery_time_text与状态 |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_lottery_time_infer.py；tests/test_lottery_time_default.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-010；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | 推断日期不是官方开奖证明，不能据此自动删除转发。 |

### OPS-011 发现及补充WATCH用户CLI

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-011；来源动态 → reaction转发者列表 → 空间转发占比/关注粉丝数据 → 筛选监控候选 |
| 触发入口 | discover_watch_users.py；replenish_watch_users.py |
| 代码位置 | scripts/discover_watch_users.py::discover_watch_users（118）；scripts/replenish_watch_users.py::main（116） |
| 调用关系 | 来源动态 → reaction转发者列表 → 空间转发占比/关注粉丝数据 → 筛选监控候选 |
| 数据读写 | 仓库config下的 watch_users.json 或 watch_users_candidates.json（工具运行时输出，不是当前Profile的DB监控列表） |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_watch_users.py（存储覆盖，发现CLI无专属全链路测试）（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-011；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | 可请求很多个人空间；是手动工具，不属于参与实时查重；风险异常可能被占比函数吞成0。 |

### OPS-012 来源容器checkpoint人工回退

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-012；读取各UP最近2容器 → 把DS1～7 checkpoint写为第二个容器 |
| 触发入口 | python scripts/rollback_ds_containers.py |
| 代码位置 | scripts/rollback_ds_containers.py::main（35） |
| 调用关系 | 读取各UP最近2容器 → 把DS1～7 checkpoint写为第二个容器 |
| 数据读写 | 当前Profile state/source checkpoint |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_profile_writer_lease_m5.py（锁约束；回退行为无专属测试）（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-012；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | 会主动联网与回退进度，非正常失败恢复；不得用在真实用户数据审计。 |

### OPS-013 活动及状态种子导出

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-013；现有活动/state → 校验/脱离参与状态 → 种子文件；新数据根初始化可消费 |
| 触发入口 | export_activities_seed.py；export_state_seed.py |
| 代码位置 | scripts/export_activities_seed.py::main（24）；scripts/export_state_seed.py::main（23） |
| 调用关系 | 现有活动/state → 校验/脱离参与状态 → 种子文件；新数据根初始化可消费 |
| 数据读写 | 种子JSON；不是Cookie迁移 |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_activity_seed.py；tests/test_state_seed.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-013；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | 需核对输出路径，不等价跨账号合并或完整备份。 |

### OPS-014 旧JSON到SQLite显式导入

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-014；writer lease → schema校验 → 非空确认/force → 事务upsert → 成功后归档 |
| 触发入口 | python scripts/import_json_to_db.py |
| 代码位置 | scripts/import_json_to_db.py::main（42）；src/db/import_json.py::run_import（892） |
| 调用关系 | writer lease → schema校验 → 非空确认/force → 事务upsert → 成功后归档 |
| 数据读写 | 当前Profile DB与json_pre_sqlite归档 |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_sqlite_data_layer.py；tests/test_profile_writer_lease_m5.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-014；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | --force可覆盖业务数据/重导动作日志，不允许随意对生产库运行。 |

### OPS-015 旧单账号迁移包与私有分发包

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-015；旧config/data一致性DB副本 → 清理不必要临时内容 → ZIP/运行包；有profiles时拒绝旧备份模型 |
| 触发入口 | export_migrate_bundle.py；export_private_complete.py；export_private_ready.py |
| 代码位置 | scripts/export_migrate_bundle.py::build_bundle（114）；scripts/export_private_complete.py::build_complete（104）；scripts/export_private_ready.py::build_ready（160） |
| 调用关系 | 旧config/data一致性DB副本 → 清理不必要临时内容 → ZIP/运行包；有profiles时拒绝旧备份模型 |
| 数据读写 | 指定输出包可包含Cookie/LLM密钥与SQLite |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_secrets_inventory.py（敏感边界）；完整打包无本轮运行证据（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-015；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | 敏感包禁止上传公开仓库；多Profile应安全退出后完整备份数据根，不能用旧工具假称完整。 |

### OPS-016 数据层手工自检脚本

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-016；init/导入/快照/统计/幂等检查 → 打印断言 |
| 触发入口 | python scripts/handtest_data_layer.py |
| 代码位置 | scripts/handtest_data_layer.py::main（15） |
| 调用关系 | init/导入/快照/统计/幂等检查 → 打印断言 |
| 数据读写 | 所选数据根DB和种子/快照 |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_sqlite_data_layer.py（等价部分契约）（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-016；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | 有写入；必须临时根；不是只读生产健康检查。 |

### OPS-017 历史JS拆分TS一次性迁移工具

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-017；读取旧web/static/app.js → 切分声明/side effects → 覆写TS模块/状态/API/main |
| 触发入口 | python scripts/migrate_frontend_app.py |
| 代码位置 | scripts/migrate_frontend_app.py::main（449） |
| 调用关系 | 读取旧web/static/app.js → 切分声明/side effects → 覆写TS模块/状态/API/main |
| 数据读写 | web/frontend/src多个源文件 |
| 当前状态 | 历史遗留显式脚本，不属于现行构建 |
| 已有测试 | 无当前迁移专属回归（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-017；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | 旧app.js已移除；现在不是常规构建步骤，重新执行可能破坏稳定前端。 |

### OPS-018 品牌图标生成与同步

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-018；正式SVG → 栅格图 → ICO/ICNS/前端favicon同步 |
| 触发入口 | render_brand_png.py；sync_brand_icons.py；各平台generate_icon/generate_icns |
| 代码位置 | scripts/render_brand_png.py::main（14）；scripts/sync_brand_icons.py::main（28）；packaging/icon_raster.py；packaging/windows/generate_icon.py；packaging/macos/generate_icns.py |
| 调用关系 | 正式SVG → 栅格图 → ICO/ICNS/前端favicon同步 |
| 数据读写 | 受控图标输出资源 |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_system_tray_v110.py（spec资源包含断言）（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-018；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | 本轮不生成资源、不擅自替换正式图标；各工具有写文件副作用。 |

### OPS-019 MCP只读查询工具

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-019；MCP参数 → localhost8787 API → 包装JSON错误 → tool text |
| 触发入口 | binggo-mcp stdio：account/summary/settings/runtime/watch/activities/triple/job/logs/auto |
| 代码位置 | mcp/binggo_mcp/server.py::summary_get（72）；mcp/binggo_mcp/client.py |
| 调用关系 | MCP参数 → localhost8787 API → 包装JSON错误 → tool text |
| 数据读写 | 本地API状态；account/extras可能B站GET |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_mcp_job_identity.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-019；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | 默认端口8787固定，无完整多Profile选择接口；只读名称不保证0远程。 |

### OPS-020 MCP登录、账号刷新及登出

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-020；串行权限 → 创建登录Job/等QR → 图像输出；刷新/登出与本地ack复用HTTP |
| 触发入口 | account_login/login_qrcode/cancel/refresh/logout/ack_at_unread |
| 代码位置 | mcp/binggo_mcp/server.py::account_login（283） / account_logout；mcp/binggo_mcp/jobs.py |
| 调用关系 | 串行权限 → 创建登录Job/等QR → 图像输出；刷新/登出与本地ack复用HTTP |
| 数据读写 | 当前固定端口账号Cookie/状态；QR非持久授权 |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_mcp_job_identity.py；tests/test_job_runner_login.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-020；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | 登陆取消仅针对login Job；调用会联网或修改登录，不在审计中执行。 |

### OPS-021 MCP任务与调度控制

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-021；串行 → POST/api/jobs → jobID绑定轮询 → 终态；单Profile runner互斥 |
| 触发入口 | job_refresh_watch/status/all/source/participate/triple；auto_start/stop |
| 代码位置 | mcp/binggo_mcp/server.py::job_participate（436） / job_refresh_all；mcp/binggo_mcp/jobs.py |
| 调用关系 | 串行 → POST/api/jobs → jobID绑定轮询 → 终态；单Profile runner互斥 |
| 数据读写 | jobs；对应扫描/参与持久记录 |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_mcp_job_identity.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-021；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | MCP participate没有V112 Web本次人工参数；不得假称能解UNKNOWN；实际可能B站POST。 |

### OPS-022 MCP设置、更新检查及诊断导出

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-022；tools → 相应设置/更新/导出API → 返回状态 |
| 触发入口 | participate_text_save/reset/mode；llm_settings_save/test；updates_check；diagnostics_export；watch_user_add/remove |
| 代码位置 | mcp/binggo_mcp/server.py::participate_text_save（494） / llm_settings_test / diagnostics_export |
| 调用关系 | tools → 相应设置/更新/导出API → 返回状态 |
| 数据读写 | 配置/共享llm.env/本地诊断包 |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_mcp_job_identity.py；tests/test_llm_settings.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-022；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | LLM test外网；更新查GitHub；watch添加可校验远端；不得把工具集当离线。 |

### OPS-023 MCP串行等待与Job身份保护

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-023；async锁 → 任务创建id → 1秒轮询最长1小时；登录QR90秒超时/进度 |
| 触发入口 | mcp jobs/serial内部调用 |
| 代码位置 | mcp/binggo_mcp/jobs.py:1；mcp/binggo_mcp/serial.py |
| 调用关系 | async锁 → 任务创建id → 1秒轮询最长1小时；登录QR90秒超时/进度 |
| 数据读写 | 当前Job身份，不另建业务队列 |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | tests/test_mcp_job_identity.py（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-023；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | MCP等待超时不代表业务Job终止；instructions旧冲突建议与新版契约需校对。 |

### OPS-024 CI、发布工作流与站点更新

| 字段 | 已核实内容 |
|---|---|
| 编号/作用 | OPS-024；Python/前端静态Gate → 各平台构建/上传release；文档Pages与star-history资源更新 |
| 触发入口 | .github/workflows推送/PR/tag/定时工作流 |
| 代码位置 | .github/workflows/ci.yml:1；.github/workflows/pages.yml；.github/workflows/release-windows.yml；.github/workflows/release-macos.yml；.github/workflows/star-history.yml |
| 调用关系 | Python/前端静态Gate → 各平台构建/上传release；文档Pages与star-history资源更新 |
| 数据读写 | CI临时工作区/远程发布产物 |
| 当前状态 | 在用（显式CLI/开发入口，非自动Scheduler） |
| 已有测试 | workflow源码静态检查；无本轮GitHub实际执行（已读相关断言；本轮未执行） |
| 人工测试 | 见测试计划 OPS-024；仅在隔离副本与临时数据根中按本项入口验证，远端全部替换为fixture；不要对真实Profile执行。 |
| 风险/边界 | GitHub发布不是本轮授权；构建通过不能代替Windows安装人工验收。 |


## 扫描功能矩阵与完整数据流

以下18项覆盖所有真实来源、补漏、通知恢复与编排入口；SC-019～023为共享规范化/分类/补齐/状态阶段。DS8不存在于当前注册表；DS9不是DS-8的别名。接口仅为源码常量与调用证据，本轮未访问平台。

| 编号/来源 | 接口与内容 | 数量边界 | 判型/LLM/去重 | 持久化/断点 | 失败与重叠 |
|---|---|---|---|---|---|
| SC-001 DS-1 小助理最新视频描述 | /x/space/wbi/arc/search；旧/space/arc/search回退；/x/web-interface/view和video页面HTML；UP885439最新视频描述中的t动态链接 | 最新1视频；不连续遍历历史视频 | 复用公共判型；canonical/已有DB去重，普通转发LLM | DB source_states / ds_check快照（state_store与db/snapshots） | 源失败不标完成；公共Pipeline内未落盘分类可重做；只检查最新 1 个视频，间隔内连续发布可能漏掉中间视频；没有自动无限追溯。 |
| SC-002 DS-2 番茄薯条喵（历史模块名fanqiao）近期专栏 | /x/space/article(mid=3546836235193146,pn=1,ps=5,sort=publish_time) → /x/article/view(id=cv)；专栏结构化opus正文；源参数决定输出cv/opus容器形式 | 第一页最近5篇；首次只有最新1篇；无自动第2页 | 复用公共判型；canonical/已有DB去重，普通转发LLM | DB source checkpoint、ds_check.source_completed_containers/失败列表 | 已知容器读取失败保留partial；正式risk保存候选快照后raise；未知异常raise；有界最近 5 篇，不是全文历史；未找到旧边界不会伪造完成，也不会自动翻第 2 页。 |
| SC-003 DS-4 J君名近期专栏 | /x/space/article(mid=126038161,pn=1,ps=5,sort=publish_time) → /x/article/view(id=cv)；专栏结构化opus正文；源参数决定输出cv/opus容器形式 | 第一页最近5篇；首次只有最新1篇；无自动第2页 | 复用公共判型；canonical/已有DB去重，普通转发LLM | DB source checkpoint、ds_check.source_completed_containers/失败列表 | 已知容器读取失败保留partial；正式risk保存候选快照后raise；未知异常raise；有界最近 5 篇，不是全文历史；未找到旧边界不会伪造完成，也不会自动翻第 2 页。 |
| SC-004 DS-5 互动娘近期专栏 | /x/space/article(mid=3546776042736296,pn=1,ps=5,sort=publish_time) → /x/article/view(id=cv)；专栏结构化opus正文；源参数决定输出cv/opus容器形式 | 第一页最近5篇；首次只有最新1篇；无自动第2页 | 复用公共判型；canonical/已有DB去重，普通转发LLM | DB source checkpoint、ds_check.source_completed_containers/失败列表 | 已知容器读取失败保留partial；正式risk保存候选快照后raise；未知异常raise；有界最近 5 篇，不是全文历史；未找到旧边界不会伪造完成，也不会自动翻第 2 页。 |
| SC-005 DS-6 糯米近期专栏 | /x/space/article(mid=492426375,pn=1,ps=5,sort=publish_time) → /x/article/view(id=cv)；专栏结构化opus正文；源参数决定输出cv/opus容器形式 | 第一页最近5篇；首次只有最新1篇；无自动第2页 | 复用公共判型；canonical/已有DB去重，普通转发LLM | DB source checkpoint、ds_check.source_completed_containers/失败列表 | 已知容器读取失败保留partial；正式risk保存候选快照后raise；未知异常raise；有界最近 5 篇，不是全文历史；未找到旧边界不会伪造完成，也不会自动翻第 2 页。 |
| SC-006 DS-7 大锦鲤近期专栏 | /x/space/article(mid=226257459,pn=1,ps=5,sort=publish_time) → /x/article/view(id=cv)；专栏结构化opus正文；源参数决定输出cv/opus容器形式 | 第一页最近5篇；首次只有最新1篇；无自动第2页 | 复用公共判型；canonical/已有DB去重，普通转发LLM | DB source checkpoint、ds_check.source_completed_containers/失败列表 | 已知容器读取失败保留partial；正式risk保存候选快照后raise；未知异常raise；有界最近 5 篇，不是全文历史；未找到旧边界不会伪造完成，也不会自动翻第 2 页。 |
| SC-007 DS-3 工具人外层专栏与嵌套合集 | /x/space/article最新cv → /x/article/view → /x/polymer/web-dynamic/v1/opus/detail；外层专栏root链接；非明确直达t的root Opus先核对结构，再展开工具人合集 | 外层最新1cv；每root需核对Opus至多一次；内部55叶子不会逐个再探测 | 复用公共判型；canonical/已有DB去重，普通转发LLM | DB ds_check的ds3_expanded_containers/source_complete/失败URL及source checkpoint | 坏容器记URL+reason不回退容器；risk保存已提取候选后raise并停止detail/Pipeline；未知raise；-352/-509/HTTP429 后停止同类 detail；普通已知读取失败可 skip，未知异常抛出。 |
| SC-008 DS9 多话题动态扫描 | /x/polymer/web-dynamic/v1/feed/topic（现代feed）；历史兼容topic_svr调用在客户端保留；启用话题；默认1261901/1055656/47801；独立原帖和包装动态 | 20项/页，最多3页/话题；话题数随配置 | 复用公共判型；canonical/已有DB去重，普通转发LLM | Profile ds9_topics状态；每话题fp/incomplete；source observations与审计DB/JSON | 普通话题失败保留该话题旧边界；formal停止；原帖补读不完整保留retryable；只是最多3页有界窗口；移动头部超出范围仍可能遗漏旧内容；没有跨所有账号的统一去重。 |
| SC-009 Following 关注动态最新窗口 | /x/polymer/web-dynamic/v1/feed/all(type=all,page,offset,retries=0)；当前UID关注流；本人动态排除；FORWARD外层和原帖分别处理 | 首次1页；最新窗口最多20页；不强行假定平台固定页大小；旧无分页字段首屏最多30项 | 复用公共判型；canonical/已有DB去重，普通转发LLM | Profile following_feed_state.json v2 + activities | 读取畸形/缺失登录可failed/incomplete；异常raise；partial可返回success但保留旧cp；最新窗口最多20页；某些读取失败返回 success 但不提交 checkpoint，不能仅靠 UI 完成判断全覆盖。 |
| SC-010 Following 历史补漏与 recent catch-up | /x/polymer/web-dynamic/v1/feed/all(type=all,page,offset,retries=0)；recent积压与15天history是两条独立固定目标窗口 | recent每轮最多5页；history每轮最多5页；与最新窗口独立 | 复用公共判型；canonical/已有DB去重，普通转发LLM | 同一v2状态的recent_offset/target与history_offset/target/完成字段 | V97最新hard cap不会覆盖已有history游标；失败不能越过未处理内容；15天范围之外不保证发现；分页重读是保守恢复成本，不等于重复参与。 |
| SC-011 Following 每25条分类落盘 | 不新增扫描API；每批分类复用detail/notice等公共请求；已经收集完成的Following规范化候选 | 25条一批，workers=1；整轮共享PipelineBatchContext、seen与breaker | 复用公共判型；canonical/已有DB去重，普通转发LLM | 每批append_activities提交到Profile DB；没有新的分页checkpoint | 批次N异常只丢未完成批次内内存结果，N以前成功commit仍在；DB失败不推进cp；分页获取阶段失败尚未进入分类；非抽奖未永久缓存，失败批次及非抽奖仍可重复分类。 |
| SC-012 WATCH 指定UP转发监控 | /x/polymer/web-dynamic/v1/feed/space(host_mid,offset,WBI)；watch_users中UP顶层FORWARD的orig.id_str；非全部关注流 | 20项/页；每UP最多30页；最近10天/本轮固定窗口 | 复用公共判型；canonical/已有DB去重，普通转发LLM | watch_users DB；DB WATCH checkpoint；Profile watch_resume.json | 普通分页外层最多4次有限尝试+客户端有限读取重试；formal立即传播；cap保存resume；不是完整关注列表，也不分类UP所有原创；与Following/DS有重叠。 |
| SC-013 WATCH 超上限与失败断点续扫 | /x/polymer/web-dynamic/v1/feed/space(host_mid,offset,WBI)；WATCH未完成UP/分页积压与持久pending links | 有resume时最多1页新头+剩余29页旧窗口；不是无界扫描 | 复用公共判型；canonical/已有DB去重，普通转发LLM | 每UP offset/complete、固定start/end、pendinglinks原子JSON；DB WATCH时间 | 异常/上限保存可靠分页和候选；不误提交完成checkpoint；offset依赖平台分页，循环/畸形fail-closed；不能以页面‘完成’替代source_complete检查。 |
| SC-014 Winner @提及和回复中奖线索 | api.bilibili.com/x/msgfeed/at、/reply；@提及与回复文本里的中奖线索，不是抽奖活动抓取 | 20/页、每源最多20页；baseline1页；固定head catch-up | 稳定消息键；本地关键词，不走抽奖LLM | Profile winner_scan_state.json、winner_candidates.json、通知claim/outbox | 普通错误/异常按源结果保留未完；formal传播；昵称补齐局部捕获例外见D-004；不走活动分类Pipeline、不调用LLM；通知可能外部HTTP；词匹配存在误报/漏报。 |
| SC-015 Winner 未读私信深检 | api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions；/svr_sync/v1/svr_sync/fetch_session_msgs；必要昵称/x/space/acc/info；未读私信会话中奖词匹配，过滤系统/确定自动回复 | 会话20/页最多20页；每会话最多20页，每次消息最多20（按unread缩小） | 稳定消息键；本地关键词，不走抽奖LLM | 每会话seqno状态、候选、自动回复键、昵称7天/500项cache | formal传播；昵称仅停止后续昵称补读；会话列表cap未实现独立持久追赶cursor；昵称补充请求遇正式风险只停止昵称查找而吞异常，是否应全局暂停另见疑点；会话列表hard cap未持久resume。 |
| SC-016 Winner 候选通知中断恢复 | 无B站新扫描；四渠道通知HTTP是外部写副作用；Winner候选到通知的持久交付，不是参与动作 | 按未完成preparation/配置渠道逐项；pending候选不因500裁剪直接丢弃 | 稳定消息键；本地关键词，不走抽奖LLM | winner_candidates通知准备；notify_outbox/claims/tombstones | dispatching前落盘；进程中断后delivery_unknown不自动重发；不能声称exactly-once送达；这是通知恢复，不是B站参与重试；超过500候选的保留方向另列风险。 |
| SC-017 串行更新所有数据源 | 顺序复用所有注册来源API（DS1～7及DS9）；一键更新所有DS卡片对应的完整单源流程 | 8源串行；每源仍有自己的数量上限；不是无限源/全历史 | 复用公共判型；canonical/已有DB去重，普通转发LLM | 每源DB snapshot/activities/checkpoint及Job汇总 | 普通单源失败记结果后继续；formal/取消立即停止尾部；已保存源不丢；当前注册8个来源，包括DS9、没有DS8；函数注释仍写DS1～7，不能按旧注释缩小实际范围。普通单源失败继续，正式风险/取消停止后续源。 |
| SC-018 自动刷新批次及内部 refresh_all | refresh_all注册来源并行检查 → 公共合并分类；refresh_watch → 本地refresh_status；Scheduler固定refresh批次，也有旧API/CLI入口 | refresh_all检查8源；Pipeline默认workers4；三阶段顺序执行；V116只batch/following错峰 | 复用公共判型；canonical/已有DB去重，普通转发LLM | 各源snapshot/checkpoint/activities；scheduler_slots/auto_remote_state | formal阻止后续远端请求；partial不假推进source完成；不抢占已有Runner Job；与UI串行更新全部入口范围不同；旧CLI/MCP接口需核对版本和端口。 |

### 数据流（实际分叉）

```text
Web / Scheduler / CLI（入口语义不同，见入口索引）
  ├─ DS1～7、DS9 → 来源check → 结构化链接/原帖候选 → CheckResult快照
  │    → normalize_activity_id / canonical → 初始DB与本轮去重
  │    → classify_new_link（失效/充电跳过、official notice、reserve、普通转发LLM）
  │    → 详情/奖品/条件补齐 → 状态计算 → 最终并发DB查重 → append_activities commit
  │    → 仅source_complete且无checkpoint-blocking项时提交来源checkpoint
  ├─ Following → 最新分页+独立recent/history → FORWARD外层和一层orig分别候选
  │    → 25条批次（共享context；每批上述公共Pipeline及commit）
  │    → 批次全部可靠完成+连续覆盖证明 → caller更新对应游标
  ├─ WATCH → 固定窗口/各UP分页 → 持久resume/pendinglinks → 公共Pipeline
  │    → 全部UP可靠覆盖才提交WATCH完成边界
  └─ Winner @ / reply / DM → 本地中奖词/来源过滤 → 稳定消息key候选
       → 候选+通知准备持久 → claim/outbox → 通知渠道HTTP → 独立消息checkpoint
       （不写抽奖活动、不调用LLM、不参与、不标消息已读）
```

动态数据的source snapshot、state_store及LLM cache多数已是SQLite适配层，即使常量名为*.json也不能据此推定实际文件写入。Following/WATCH/Winner的专用JSON状态则保留各自契约。扫描发现同一动态可来自多个源，这是来源重叠，不应变成重复参与。

### 20问逐项追踪

下面每项均列20问；代码位置对应功能表及文件覆盖清单。成功条件区分‘Job终态成功’和‘窗口连续覆盖/可推进checkpoint’，两者不是同义词。

#### SC-001：DS-1 小助理最新视频描述

| 问题 | 源码结论 |
|---|---|
| 1. 名称 | DS-1 小助理最新视频描述 |
| 2. 接口/页面 | /x/space/wbi/arc/search；旧/space/arc/search回退；/x/web-interface/view和video页面HTML |
| 3. 内容范围 | UP885439最新视频描述中的t动态链接 |
| 4. 触发者 | 数据源单源更新；串行更新全部；自动 refresh；check_ds1 CLI |
| 5. 入口位置 | src/sources/ds1_xiaozhuli.py::check_update（24）；src/sources/common.py；web/actions.py::DS_HANDLERS（322） |
| 6. 调用链 | UP 885439 → WBI 投稿列表（失败时旧接口）→ 最新视频详情/HTML 回退 → 描述动态链接 → 公共 Pipeline |
| 7. 数量/分页 | 最新1视频；不连续遍历历史视频 |
| 8. 进度存储 | DB source_states / ds_check快照（state_store与db/snapshots） |
| 9. 已扫描判据 | 当前bvid容器等于已完成checkpoint则复用/跳过 |
| 10. 去重 | 原始动态ID→canonical；本轮seen、初始activities、最终fresh DB复查；Following跨批context |
| 11. 抽奖识别 | 公共classify_new_link：坏动态/充电→官方互动→非直播预约→普通转发→非抽奖 |
| 12. LLM条件 | 普通转发判型/内容结构提取可调用LLM；失败不等于非抽奖；已有可靠缓存可复用 |
| 13. 最终数据 | DB source_states / ds_check快照（state_store与db/snapshots） |
| 14. 成功条件 | 完整Pipeline成功且满足checkpoint提交条件 |
| 15. 中断/部分成功 | 源失败不标完成；公共Pipeline内未落盘分类可重做 |
| 16. 重启恢复 | DB容器/快照保留；未完成再次检查 |
| 17. 旧活动遗漏边界 | 只检查最新 1 个视频，间隔内连续发布可能漏掉中间视频；没有自动无限追溯。 |
| 18. 重复请求/分类 | 已存有效活动可复用；未完成批次、非抽奖和分页获取可能重做，不能称零请求重试 |
| 19. 功能重叠 | DS/Following/WATCH可能发现同一original；共享canonical去重但各自抓取仍有请求重叠 |
| 20. 多账号/调度 | 当前Profile Runner扫描优先/忙门禁；跨Profile独立；仅自动refresh/following rank×15分钟，没有全局任务锁 |

#### SC-002：DS-2 番茄薯条喵（历史模块名fanqiao）近期专栏

| 问题 | 源码结论 |
|---|---|
| 1. 名称 | DS-2 番茄薯条喵（历史模块名fanqiao）近期专栏 |
| 2. 接口/页面 | /x/space/article(mid=3546836235193146,pn=1,ps=5,sort=publish_time) → /x/article/view(id=cv) |
| 3. 内容范围 | 专栏结构化opus正文；源参数决定输出cv/opus容器形式 |
| 4. 触发者 | 单源更新；串行更新全部；自动 refresh；check_ds2 CLI |
| 5. 入口位置 | src/sources/ds2_fanqiao.py::check_update（19）；src/sources/recent_articles.py::check_recent_articles（61） |
| 6. 调用链 | UP 3546836235193146 → /x/space/article 第一页最近 5 篇 → checkpoint 前的新文章 → 正文节点/URL提取 → 公共 Pipeline |
| 7. 数量/分页 | 第一页最近5篇；首次只有最新1篇；无自动第2页 |
| 8. 进度存储 | DB source checkpoint、ds_check.source_completed_containers/失败列表 |
| 9. 已扫描判据 | 旧cv边界在最近5篇；与旧边界对应的成功容器缓存 |
| 10. 去重 | 原始动态ID→canonical；本轮seen、初始activities、最终fresh DB复查；Following跨批context |
| 11. 抽奖识别 | 公共classify_new_link：坏动态/充电→官方互动→非直播预约→普通转发→非抽奖 |
| 12. LLM条件 | 普通转发判型/内容结构提取可调用LLM；失败不等于非抽奖；已有可靠缓存可复用 |
| 13. 最终数据 | DB source checkpoint、ds_check.source_completed_containers/失败列表 |
| 14. 成功条件 | 找到旧边界、待处理容器全完成并可靠入库才提交 |
| 15. 中断/部分成功 | 已知容器读取失败保留partial；正式risk保存候选快照后raise；未知异常raise |
| 16. 重启恢复 | 复用成功容器，仅重取失败项；边界不在窗口仍incomplete |
| 17. 旧活动遗漏边界 | 有界最近 5 篇，不是全文历史；未找到旧边界不会伪造完成，也不会自动翻第 2 页。 |
| 18. 重复请求/分类 | 已存有效活动可复用；未完成批次、非抽奖和分页获取可能重做，不能称零请求重试 |
| 19. 功能重叠 | DS/Following/WATCH可能发现同一original；共享canonical去重但各自抓取仍有请求重叠 |
| 20. 多账号/调度 | 当前Profile Runner扫描优先/忙门禁；跨Profile独立；仅自动refresh/following rank×15分钟，没有全局任务锁 |

#### SC-003：DS-4 J君名近期专栏

| 问题 | 源码结论 |
|---|---|
| 1. 名称 | DS-4 J君名近期专栏 |
| 2. 接口/页面 | /x/space/article(mid=126038161,pn=1,ps=5,sort=publish_time) → /x/article/view(id=cv) |
| 3. 内容范围 | 专栏结构化opus正文；源参数决定输出cv/opus容器形式 |
| 4. 触发者 | 单源更新；串行更新全部；自动 refresh；check_ds4 CLI |
| 5. 入口位置 | src/sources/ds4_junming.py::check_update（22）；src/sources/recent_articles.py::check_recent_articles（61） |
| 6. 调用链 | UP 126038161 → /x/space/article 第一页最近 5 篇 → checkpoint 前的新文章 → 正文节点/URL提取 → 公共 Pipeline |
| 7. 数量/分页 | 第一页最近5篇；首次只有最新1篇；无自动第2页 |
| 8. 进度存储 | DB source checkpoint、ds_check.source_completed_containers/失败列表 |
| 9. 已扫描判据 | 旧cv边界在最近5篇；与旧边界对应的成功容器缓存 |
| 10. 去重 | 原始动态ID→canonical；本轮seen、初始activities、最终fresh DB复查；Following跨批context |
| 11. 抽奖识别 | 公共classify_new_link：坏动态/充电→官方互动→非直播预约→普通转发→非抽奖 |
| 12. LLM条件 | 普通转发判型/内容结构提取可调用LLM；失败不等于非抽奖；已有可靠缓存可复用 |
| 13. 最终数据 | DB source checkpoint、ds_check.source_completed_containers/失败列表 |
| 14. 成功条件 | 找到旧边界、待处理容器全完成并可靠入库才提交 |
| 15. 中断/部分成功 | 已知容器读取失败保留partial；正式risk保存候选快照后raise；未知异常raise |
| 16. 重启恢复 | 复用成功容器，仅重取失败项；边界不在窗口仍incomplete |
| 17. 旧活动遗漏边界 | 有界最近 5 篇，不是全文历史；未找到旧边界不会伪造完成，也不会自动翻第 2 页。 |
| 18. 重复请求/分类 | 已存有效活动可复用；未完成批次、非抽奖和分页获取可能重做，不能称零请求重试 |
| 19. 功能重叠 | DS/Following/WATCH可能发现同一original；共享canonical去重但各自抓取仍有请求重叠 |
| 20. 多账号/调度 | 当前Profile Runner扫描优先/忙门禁；跨Profile独立；仅自动refresh/following rank×15分钟，没有全局任务锁 |

#### SC-004：DS-5 互动娘近期专栏

| 问题 | 源码结论 |
|---|---|
| 1. 名称 | DS-5 互动娘近期专栏 |
| 2. 接口/页面 | /x/space/article(mid=3546776042736296,pn=1,ps=5,sort=publish_time) → /x/article/view(id=cv) |
| 3. 内容范围 | 专栏结构化opus正文；源参数决定输出cv/opus容器形式 |
| 4. 触发者 | 单源更新；串行更新全部；自动 refresh；check_ds5 CLI |
| 5. 入口位置 | src/sources/ds5_hudong.py::check_update（15）；src/sources/recent_articles.py::check_recent_articles（61） |
| 6. 调用链 | UP 3546776042736296 → /x/space/article 第一页最近 5 篇 → checkpoint 前的新文章 → 正文节点/URL提取 → 公共 Pipeline |
| 7. 数量/分页 | 第一页最近5篇；首次只有最新1篇；无自动第2页 |
| 8. 进度存储 | DB source checkpoint、ds_check.source_completed_containers/失败列表 |
| 9. 已扫描判据 | 旧cv边界在最近5篇；与旧边界对应的成功容器缓存 |
| 10. 去重 | 原始动态ID→canonical；本轮seen、初始activities、最终fresh DB复查；Following跨批context |
| 11. 抽奖识别 | 公共classify_new_link：坏动态/充电→官方互动→非直播预约→普通转发→非抽奖 |
| 12. LLM条件 | 普通转发判型/内容结构提取可调用LLM；失败不等于非抽奖；已有可靠缓存可复用 |
| 13. 最终数据 | DB source checkpoint、ds_check.source_completed_containers/失败列表 |
| 14. 成功条件 | 找到旧边界、待处理容器全完成并可靠入库才提交 |
| 15. 中断/部分成功 | 已知容器读取失败保留partial；正式risk保存候选快照后raise；未知异常raise |
| 16. 重启恢复 | 复用成功容器，仅重取失败项；边界不在窗口仍incomplete |
| 17. 旧活动遗漏边界 | 有界最近 5 篇，不是全文历史；未找到旧边界不会伪造完成，也不会自动翻第 2 页。 |
| 18. 重复请求/分类 | 已存有效活动可复用；未完成批次、非抽奖和分页获取可能重做，不能称零请求重试 |
| 19. 功能重叠 | DS/Following/WATCH可能发现同一original；共享canonical去重但各自抓取仍有请求重叠 |
| 20. 多账号/调度 | 当前Profile Runner扫描优先/忙门禁；跨Profile独立；仅自动refresh/following rank×15分钟，没有全局任务锁 |

#### SC-005：DS-6 糯米近期专栏

| 问题 | 源码结论 |
|---|---|
| 1. 名称 | DS-6 糯米近期专栏 |
| 2. 接口/页面 | /x/space/article(mid=492426375,pn=1,ps=5,sort=publish_time) → /x/article/view(id=cv) |
| 3. 内容范围 | 专栏结构化opus正文；源参数决定输出cv/opus容器形式 |
| 4. 触发者 | 单源更新；串行更新全部；自动 refresh；check_ds6 CLI |
| 5. 入口位置 | src/sources/ds6_nuomi.py::check_update（15）；src/sources/recent_articles.py::check_recent_articles（61） |
| 6. 调用链 | UP 492426375 → /x/space/article 第一页最近 5 篇 → checkpoint 前的新文章 → 正文节点/URL提取 → 公共 Pipeline |
| 7. 数量/分页 | 第一页最近5篇；首次只有最新1篇；无自动第2页 |
| 8. 进度存储 | DB source checkpoint、ds_check.source_completed_containers/失败列表 |
| 9. 已扫描判据 | 旧cv边界在最近5篇；与旧边界对应的成功容器缓存 |
| 10. 去重 | 原始动态ID→canonical；本轮seen、初始activities、最终fresh DB复查；Following跨批context |
| 11. 抽奖识别 | 公共classify_new_link：坏动态/充电→官方互动→非直播预约→普通转发→非抽奖 |
| 12. LLM条件 | 普通转发判型/内容结构提取可调用LLM；失败不等于非抽奖；已有可靠缓存可复用 |
| 13. 最终数据 | DB source checkpoint、ds_check.source_completed_containers/失败列表 |
| 14. 成功条件 | 找到旧边界、待处理容器全完成并可靠入库才提交 |
| 15. 中断/部分成功 | 已知容器读取失败保留partial；正式risk保存候选快照后raise；未知异常raise |
| 16. 重启恢复 | 复用成功容器，仅重取失败项；边界不在窗口仍incomplete |
| 17. 旧活动遗漏边界 | 有界最近 5 篇，不是全文历史；未找到旧边界不会伪造完成，也不会自动翻第 2 页。 |
| 18. 重复请求/分类 | 已存有效活动可复用；未完成批次、非抽奖和分页获取可能重做，不能称零请求重试 |
| 19. 功能重叠 | DS/Following/WATCH可能发现同一original；共享canonical去重但各自抓取仍有请求重叠 |
| 20. 多账号/调度 | 当前Profile Runner扫描优先/忙门禁；跨Profile独立；仅自动refresh/following rank×15分钟，没有全局任务锁 |

#### SC-006：DS-7 大锦鲤近期专栏

| 问题 | 源码结论 |
|---|---|
| 1. 名称 | DS-7 大锦鲤近期专栏 |
| 2. 接口/页面 | /x/space/article(mid=226257459,pn=1,ps=5,sort=publish_time) → /x/article/view(id=cv) |
| 3. 内容范围 | 专栏结构化opus正文；源参数决定输出cv/opus容器形式 |
| 4. 触发者 | 单源更新；串行更新全部；自动 refresh；check_ds7 CLI |
| 5. 入口位置 | src/sources/ds7_dajinli.py::check_update（15）；src/sources/recent_articles.py::check_recent_articles（61） |
| 6. 调用链 | UP 226257459 → /x/space/article 第一页最近 5 篇 → checkpoint 前的新文章 → 正文节点/URL提取 → 公共 Pipeline |
| 7. 数量/分页 | 第一页最近5篇；首次只有最新1篇；无自动第2页 |
| 8. 进度存储 | DB source checkpoint、ds_check.source_completed_containers/失败列表 |
| 9. 已扫描判据 | 旧cv边界在最近5篇；与旧边界对应的成功容器缓存 |
| 10. 去重 | 原始动态ID→canonical；本轮seen、初始activities、最终fresh DB复查；Following跨批context |
| 11. 抽奖识别 | 公共classify_new_link：坏动态/充电→官方互动→非直播预约→普通转发→非抽奖 |
| 12. LLM条件 | 普通转发判型/内容结构提取可调用LLM；失败不等于非抽奖；已有可靠缓存可复用 |
| 13. 最终数据 | DB source checkpoint、ds_check.source_completed_containers/失败列表 |
| 14. 成功条件 | 找到旧边界、待处理容器全完成并可靠入库才提交 |
| 15. 中断/部分成功 | 已知容器读取失败保留partial；正式risk保存候选快照后raise；未知异常raise |
| 16. 重启恢复 | 复用成功容器，仅重取失败项；边界不在窗口仍incomplete |
| 17. 旧活动遗漏边界 | 有界最近 5 篇，不是全文历史；未找到旧边界不会伪造完成，也不会自动翻第 2 页。 |
| 18. 重复请求/分类 | 已存有效活动可复用；未完成批次、非抽奖和分页获取可能重做，不能称零请求重试 |
| 19. 功能重叠 | DS/Following/WATCH可能发现同一original；共享canonical去重但各自抓取仍有请求重叠 |
| 20. 多账号/调度 | 当前Profile Runner扫描优先/忙门禁；跨Profile独立；仅自动refresh/following rank×15分钟，没有全局任务锁 |

#### SC-007：DS-3 工具人外层专栏与嵌套合集

| 问题 | 源码结论 |
|---|---|
| 1. 名称 | DS-3 工具人外层专栏与嵌套合集 |
| 2. 接口/页面 | /x/space/article最新cv → /x/article/view → /x/polymer/web-dynamic/v1/opus/detail |
| 3. 内容范围 | 外层专栏root链接；非明确直达t的root Opus先核对结构，再展开工具人合集 |
| 4. 触发者 | DS-3 更新；串行全部；自动 refresh；check_ds3 |
| 5. 入口位置 | src/sources/ds3_gongjuren.py::check_update（313） / expand_container_links |
| 6. 调用链 | UP 100680137 最新 cv → 外层正文既有提取 → 明确 Opus 容器 detail 一次 → 动态叶子去重 → 公共 Pipeline |
| 7. 数量/分页 | 外层最新1cv；每root需核对Opus至多一次；内部55叶子不会逐个再探测 |
| 8. 进度存储 | DB ds_check的ds3_expanded_containers/source_complete/失败URL及source checkpoint |
| 9. 已扫描判据 | 完成cv边界；同cv partial触发补读并复用成功容器 |
| 10. 去重 | 原始动态ID→canonical；本轮seen、初始activities、最终fresh DB复查；Following跨批context |
| 11. 抽奖识别 | 公共classify_new_link：坏动态/充电→官方互动→非直播预约→普通转发→非抽奖 |
| 12. LLM条件 | 普通转发判型/内容结构提取可调用LLM；失败不等于非抽奖；已有可靠缓存可复用 |
| 13. 最终数据 | DB ds_check的ds3_expanded_containers/source_complete/失败URL及source checkpoint |
| 14. 成功条件 | 全部需展开root完成，公共分类成功才提交完整容器checkpoint |
| 15. 中断/部分成功 | 坏容器记URL+reason不回退容器；risk保存已提取候选后raise并停止detail/Pipeline；未知raise |
| 16. 重启恢复 | 普通刷新能补读同cv失败root，不被相同容器完成条件误跳过 |
| 17. 旧活动遗漏边界 | -352/-509/HTTP429 后停止同类 detail；普通已知读取失败可 skip，未知异常抛出。 |
| 18. 重复请求/分类 | 已存有效活动可复用；未完成批次、非抽奖和分页获取可能重做，不能称零请求重试 |
| 19. 功能重叠 | DS/Following/WATCH可能发现同一original；共享canonical去重但各自抓取仍有请求重叠 |
| 20. 多账号/调度 | 当前Profile Runner扫描优先/忙门禁；跨Profile独立；仅自动refresh/following rank×15分钟，没有全局任务锁 |

#### SC-008：DS9 多话题动态扫描

| 问题 | 源码结论 |
|---|---|
| 1. 名称 | DS9 多话题动态扫描 |
| 2. 接口/页面 | /x/polymer/web-dynamic/v1/feed/topic（现代feed）；历史兼容topic_svr调用在客户端保留 |
| 3. 内容范围 | 启用话题；默认1261901/1055656/47801；独立原帖和包装动态 |
| 4. 触发者 | DS9卡片更新；refresh_source DS-9；refresh_all/自动 refresh |
| 5. 入口位置 | src/sources/ds9_topics.py::check_update（609）；web/actions.py |
| 6. 调用链 | /x/polymer/web-dynamic/v1/feed/topic → 每页20、最多3页 → 原帖二次发现 → source observations → 公共 Pipeline |
| 7. 数量/分页 | 20项/页，最多3页/话题；话题数随配置 |
| 8. 进度存储 | Profile ds9_topics状态；每话题fp/incomplete；source observations与审计DB/JSON |
| 9. 已扫描判据 | 已完整fp且头部未变则跳过后页；incomplete即使同头也继续 |
| 10. 去重 | 原始动态ID→canonical；本轮seen、初始activities、最终fresh DB复查；Following跨批context |
| 11. 抽奖识别 | 公共classify_new_link：坏动态/充电→官方互动→非直播预约→普通转发→非抽奖 |
| 12. LLM条件 | 普通转发判型/内容结构提取可调用LLM；失败不等于非抽奖；已有可靠缓存可复用 |
| 13. 最终数据 | Profile ds9_topics状态；每话题fp/incomplete；source observations与审计DB/JSON |
| 14. 成功条件 | 成功话题分类/详情/入库完成才提交其pending更新；失败话题不伪完成 |
| 15. 中断/部分成功 | 普通话题失败保留该话题旧边界；formal停止；原帖补读不完整保留retryable |
| 16. 重启恢复 | 每话题状态保存；不把3页外历史当已覆盖 |
| 17. 旧活动遗漏边界 | 只是最多3页有界窗口；移动头部超出范围仍可能遗漏旧内容；没有跨所有账号的统一去重。 |
| 18. 重复请求/分类 | 已存有效活动可复用；未完成批次、非抽奖和分页获取可能重做，不能称零请求重试 |
| 19. 功能重叠 | DS/Following/WATCH可能发现同一original；共享canonical去重但各自抓取仍有请求重叠 |
| 20. 多账号/调度 | 当前Profile Runner扫描优先/忙门禁；跨Profile独立；仅自动refresh/following rank×15分钟，没有全局任务锁 |

#### SC-009：Following 关注动态最新窗口

| 问题 | 源码结论 |
|---|---|
| 1. 名称 | Following 关注动态最新窗口 |
| 2. 接口/页面 | /x/polymer/web-dynamic/v1/feed/all(type=all,page,offset,retries=0) |
| 3. 内容范围 | 当前UID关注流；本人动态排除；FORWARD外层和原帖分别处理 |
| 4. 触发者 | 数据源立即扫描关注动态；自动 following；Job following_feed_scan |
| 5. 入口位置 | src/following_feed.py::scan_following_feed（622） |
| 6. 调用链 | feed/all(type=all,retries=0) → 验证分页/offset（仅旧无分页字段首屏兼容30项上限）→ 排除本人动态 → 包装动态与一层原帖分别候选 → 25条批次 Pipeline |
| 7. 数量/分页 | 首次1页；最新窗口最多20页；不强行假定平台固定页大小；旧无分页字段首屏最多30项 |
| 8. 进度存储 | Profile following_feed_state.json v2 + activities |
| 9. 已扫描判据 | exact动态checkpoint或最多3个旧锚点，或has_more=false；offset循环/缺失不算覆盖 |
| 10. 去重 | 原始动态ID→canonical；本轮seen、初始activities、最终fresh DB复查；Following跨批context |
| 11. 抽奖识别 | 公共classify_new_link：坏动态/充电→官方互动→非直播预约→普通转发→非抽奖 |
| 12. LLM条件 | 普通转发判型/内容结构提取可调用LLM；失败不等于非抽奖；已有可靠缓存可复用 |
| 13. 最终数据 | Profile following_feed_state.json v2 + activities |
| 14. 成功条件 | 25条可靠批次完成；无blocking/partial且窗口边界可靠才更新相应进度 |
| 15. 中断/部分成功 | 读取畸形/缺失登录可failed/incomplete；异常raise；partial可返回success但保留旧cp |
| 16. 重启恢复 | 活动DB复用；独立recent backlog保持，不覆盖old history |
| 17. 旧活动遗漏边界 | 最新窗口最多20页；某些读取失败返回 success 但不提交 checkpoint，不能仅靠 UI 完成判断全覆盖。 |
| 18. 重复请求/分类 | 已存有效活动可复用；未完成批次、非抽奖和分页获取可能重做，不能称零请求重试 |
| 19. 功能重叠 | DS/Following/WATCH可能发现同一original；共享canonical去重但各自抓取仍有请求重叠 |
| 20. 多账号/调度 | 当前Profile Runner扫描优先/忙门禁；跨Profile独立；仅自动refresh/following rank×15分钟，没有全局任务锁 |

#### SC-010：Following 历史补漏与 recent catch-up

| 问题 | 源码结论 |
|---|---|
| 1. 名称 | Following 历史补漏与 recent catch-up |
| 2. 接口/页面 | /x/polymer/web-dynamic/v1/feed/all(type=all,page,offset,retries=0) |
| 3. 内容范围 | recent积压与15天history是两条独立固定目标窗口 |
| 4. 触发者 | 同一 following_feed_scan 内部按持久状态自动继续 |
| 5. 入口位置 | src/following_feed.py::scan_following_feed（622） |
| 6. 调用链 | 最新窗口 → recent 待追赶最多5页 → 独立15天 history待追溯最多5页 → 可靠批次处理 → 条件更新双游标 |
| 7. 数量/分页 | recent每轮最多5页；history每轮最多5页；与最新窗口独立 |
| 8. 进度存储 | 同一v2状态的recent_offset/target与history_offset/target/完成字段 |
| 9. 已扫描判据 | 稳定动态锚点+固定窗口终点，不仅数字offset |
| 10. 去重 | 原始动态ID→canonical；本轮seen、初始activities、最终fresh DB复查；Following跨批context |
| 11. 抽奖识别 | 公共classify_new_link：坏动态/充电→官方互动→非直播预约→普通转发→非抽奖 |
| 12. LLM条件 | 普通转发判型/内容结构提取可调用LLM；失败不等于非抽奖；已有可靠缓存可复用 |
| 13. 最终数据 | 同一v2状态的recent_offset/target与history_offset/target/完成字段 |
| 14. 成功条件 | 连续覆盖且批次落盘/无blocking才推进对应游标 |
| 15. 中断/部分成功 | V97最新hard cap不会覆盖已有history游标；失败不能越过未处理内容 |
| 16. 重启恢复 | 保存并继续原固定历史窗口；再扫最新页保证新头不遗漏 |
| 17. 旧活动遗漏边界 | 15天范围之外不保证发现；分页重读是保守恢复成本，不等于重复参与。 |
| 18. 重复请求/分类 | 已存有效活动可复用；未完成批次、非抽奖和分页获取可能重做，不能称零请求重试 |
| 19. 功能重叠 | DS/Following/WATCH可能发现同一original；共享canonical去重但各自抓取仍有请求重叠 |
| 20. 多账号/调度 | 当前Profile Runner扫描优先/忙门禁；跨Profile独立；仅自动refresh/following rank×15分钟，没有全局任务锁 |

#### SC-011：Following 每25条分类落盘

| 问题 | 源码结论 |
|---|---|
| 1. 名称 | Following 每25条分类落盘 |
| 2. 接口/页面 | 不新增扫描API；每批分类复用detail/notice等公共请求 |
| 3. 内容范围 | 已经收集完成的Following规范化候选 |
| 4. 触发者 | Following 候选收集后内部调用 |
| 5. 入口位置 | src/following_feed.py::_run_pipeline_batches（555）；src/pipeline/refresh_all_pipeline.py::PipelineBatchContext（89） |
| 6. 调用链 | 整轮候选收集 → 25条串行分类批次 → append_activities commit → 下一批；共享canonical集合/LLMbreaker |
| 7. 数量/分页 | 25条一批，workers=1；整轮共享PipelineBatchContext、seen与breaker |
| 8. 进度存储 | 每批append_activities提交到Profile DB；没有新的分页checkpoint |
| 9. 已扫描判据 | 重跑load已保存activities并canonical查重；跨批次共享集合 |
| 10. 去重 | 原始动态ID→canonical；本轮seen、初始activities、最终fresh DB复查；Following跨批context |
| 11. 抽奖识别 | 公共classify_new_link：坏动态/充电→官方互动→非直播预约→普通转发→非抽奖 |
| 12. LLM条件 | 普通转发判型/内容结构提取可调用LLM；失败不等于非抽奖；已有可靠缓存可复用 |
| 13. 最终数据 | 每批append_activities提交到Profile DB；没有新的分页checkpoint |
| 14. 成功条件 | append成功才算持久化；所有批次与覆盖证明完成后caller更新cp |
| 15. 中断/部分成功 | 批次N异常只丢未完成批次内内存结果，N以前成功commit仍在；DB失败不推进cp |
| 16. 重启恢复 | DB持久结果可复用；批次context本身不是跨重启持久缓存 |
| 17. 旧活动遗漏边界 | 分页获取阶段失败尚未进入分类；非抽奖未永久缓存，失败批次及非抽奖仍可重复分类。 |
| 18. 重复请求/分类 | 已存有效活动可复用；未完成批次、非抽奖和分页获取可能重做，不能称零请求重试 |
| 19. 功能重叠 | DS/Following/WATCH可能发现同一original；共享canonical去重但各自抓取仍有请求重叠 |
| 20. 多账号/调度 | 当前Profile Runner扫描优先/忙门禁；跨Profile独立；仅自动refresh/following rank×15分钟，没有全局任务锁 |

#### SC-012：WATCH 指定UP转发监控

| 问题 | 源码结论 |
|---|---|
| 1. 名称 | WATCH 指定UP转发监控 |
| 2. 接口/页面 | /x/polymer/web-dynamic/v1/feed/space(host_mid,offset,WBI) |
| 3. 内容范围 | watch_users中UP顶层FORWARD的orig.id_str；非全部关注流 |
| 4. 触发者 | WATCH添加列表；刷新监控动态；自动refresh第二步 |
| 5. 入口位置 | src/watch_feed.py::collect_forward_links_for_user（208）；src/watch_sync.py；web/actions.py::run_action（1219） |
| 6. 调用链 | /feed/space(host_mid) → 原始FORWARD的orig.id_str → 最近10天/本轮边界 → 公共 Pipeline |
| 7. 数量/分页 | 20项/页；每UP最多30页；最近10天/本轮固定窗口 |
| 8. 进度存储 | watch_users DB；DB WATCH checkpoint；Profile watch_resume.json |
| 9. 已扫描判据 | 按UP固定窗口、has_more/时间边界及complete |
| 10. 去重 | 原始动态ID→canonical；本轮seen、初始activities、最终fresh DB复查；Following跨批context |
| 11. 抽奖识别 | 公共classify_new_link：坏动态/充电→官方互动→非直播预约→普通转发→非抽奖 |
| 12. LLM条件 | 普通转发判型/内容结构提取可调用LLM；失败不等于非抽奖；已有可靠缓存可复用 |
| 13. 最终数据 | watch_users DB；DB WATCH checkpoint；Profile watch_resume.json |
| 14. 成功条件 | 全部UP连续覆盖并公共Pipeline无阻塞才提交WATCH完成边界 |
| 15. 中断/部分成功 | 普通分页外层最多4次有限尝试+客户端有限读取重试；formal立即传播；cap保存resume |
| 16. 重启恢复 | 续固定历史窗口，并有限检查新头；重复数据由动态ID去重 |
| 17. 旧活动遗漏边界 | 不是完整关注列表，也不分类UP所有原创；与Following/DS有重叠。 |
| 18. 重复请求/分类 | 已存有效活动可复用；未完成批次、非抽奖和分页获取可能重做，不能称零请求重试 |
| 19. 功能重叠 | DS/Following/WATCH可能发现同一original；共享canonical去重但各自抓取仍有请求重叠 |
| 20. 多账号/调度 | 当前Profile Runner扫描优先/忙门禁；跨Profile独立；仅自动refresh/following rank×15分钟，没有全局任务锁 |

#### SC-013：WATCH 超上限与失败断点续扫

| 问题 | 源码结论 |
|---|---|
| 1. 名称 | WATCH 超上限与失败断点续扫 |
| 2. 接口/页面 | /x/polymer/web-dynamic/v1/feed/space(host_mid,offset,WBI) |
| 3. 内容范围 | WATCH未完成UP/分页积压与持久pending links |
| 4. 触发者 | refresh_watch内部根据 watch_resume.json 继续 |
| 5. 入口位置 | src/watch_resume.py；src/watch_feed.py::collect_forward_links_for_user（208） |
| 6. 调用链 | 固定窗口+每UP offset/complete+pending links → 下一轮先新头再旧断点 → Pipeline → 条件退役resume |
| 7. 数量/分页 | 有resume时最多1页新头+剩余29页旧窗口；不是无界扫描 |
| 8. 进度存储 | 每UP offset/complete、固定start/end、pendinglinks原子JSON；DB WATCH时间 |
| 9. 已扫描判据 | 所有有效UP的覆盖证明，不以max_pages到达当完成 |
| 10. 去重 | 原始动态ID→canonical；本轮seen、初始activities、最终fresh DB复查；Following跨批context |
| 11. 抽奖识别 | 公共classify_new_link：坏动态/充电→官方互动→非直播预约→普通转发→非抽奖 |
| 12. LLM条件 | 普通转发判型/内容结构提取可调用LLM；失败不等于非抽奖；已有可靠缓存可复用 |
| 13. 最终数据 | 每UP offset/complete、固定start/end、pendinglinks原子JSON；DB WATCH时间 |
| 14. 成功条件 | 候选持久/分类完成后才能退役resume与推进整体边界 |
| 15. 中断/部分成功 | 异常/上限保存可靠分页和候选；不误提交完成checkpoint |
| 16. 重启恢复 | 新增UP独立处理；已移除UP按配置修剪；原窗口不被新头覆盖 |
| 17. 旧活动遗漏边界 | offset依赖平台分页，循环/畸形fail-closed；不能以页面‘完成’替代source_complete检查。 |
| 18. 重复请求/分类 | 已存有效活动可复用；未完成批次、非抽奖和分页获取可能重做，不能称零请求重试 |
| 19. 功能重叠 | DS/Following/WATCH可能发现同一original；共享canonical去重但各自抓取仍有请求重叠 |
| 20. 多账号/调度 | 当前Profile Runner扫描优先/忙门禁；跨Profile独立；仅自动refresh/following rank×15分钟，没有全局任务锁 |

#### SC-014：Winner @提及和回复中奖线索

| 问题 | 源码结论 |
|---|---|
| 1. 名称 | Winner @提及和回复中奖线索 |
| 2. 接口/页面 | api.bilibili.com/x/msgfeed/at、/reply |
| 3. 内容范围 | @提及与回复文本里的中奖线索，不是抽奖活动抓取 |
| 4. 触发者 | 中奖通知立即深检；自动 winner；Job winner_deep_scan |
| 5. 入口位置 | src/winner_scan/service.py::WinnerScanService.run（1332）/_fetch_feed_window（815）；src/message_api.py |
| 6. 调用链 | /x/msgfeed/at、reply → 每页20最多20页 → 本地关键词/黑名单 → 稳定消息键 → 候选与通知准备 → 独立游标 |
| 7. 数量/分页 | 20/页、每源最多20页；baseline1页；固定head catch-up |
| 8. 进度存储 | Profile winner_scan_state.json、winner_candidates.json、通知claim/outbox |
| 9. 已扫描判据 | 消息稳定key/时间head+每源pending catchup，不按活动DB排除 |
| 10. 去重 | 独立消息key/event+channel；不依赖activities |
| 11. 抽奖识别 | @/回复/私信本地词匹配仅为线索；通知恢复不新判型 |
| 12. LLM条件 | 不调用LLM |
| 13. 最终数据 | Profile winner_scan_state.json、winner_candidates.json、通知claim/outbox |
| 14. 成功条件 | 候选preparation及通知claim持久后才移动消息边界 |
| 15. 中断/部分成功 | 普通错误/异常按源结果保留未完；formal传播；昵称补齐局部捕获例外见D-004 |
| 16. 重启恢复 | 先恢复已保存通知准备，再扫描旧积压；有独立Winner游标 |
| 17. 旧活动遗漏边界 | 不走活动分类Pipeline、不调用LLM；通知可能外部HTTP；词匹配存在误报/漏报。 |
| 18. 重复请求/分类 | 消息页/未完会话可能重读；通知claim防重复派送，与参与guard无关 |
| 19. 功能重叠 | 与账号消息角标读入口部分重叠；Winner独立，不走DS活动库 |
| 20. 多账号/调度 | 当前Profile Runner互斥；多Profile独立且Winner不受V116错峰，跨进程请求可能重叠 |

#### SC-015：Winner 未读私信深检

| 问题 | 源码结论 |
|---|---|
| 1. 名称 | Winner 未读私信深检 |
| 2. 接口/页面 | api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions；/svr_sync/v1/svr_sync/fetch_session_msgs；必要昵称/x/space/acc/info |
| 3. 内容范围 | 未读私信会话中奖词匹配，过滤系统/确定自动回复 |
| 4. 触发者 | 同一 Winner Job 私信阶段 |
| 5. 入口位置 | src/winner_scan/service.py::_collect_dm_sessions（1261）/_fetch_dm_session_window（980）/_scan_dm_source（1141） |
| 6. 调用链 | 私信sessions最新20、最多20页 → 每未读会话消息20、最多20页 → 过滤系统/明确自动回复 → 关键词候选 |
| 7. 数量/分页 | 会话20/页最多20页；每会话最多20页，每次消息最多20（按unread缩小） |
| 8. 进度存储 | 每会话seqno状态、候选、自动回复键、昵称7天/500项cache |
| 9. 已扫描判据 | session/message稳定键与已提交seq；不执行mark_read |
| 10. 去重 | 独立消息key/event+channel；不依赖activities |
| 11. 抽奖识别 | @/回复/私信本地词匹配仅为线索；通知恢复不新判型 |
| 12. LLM条件 | 不调用LLM |
| 13. 最终数据 | 每会话seqno状态、候选、自动回复键、昵称7天/500项cache |
| 14. 成功条件 | 候选及通知准备持久后提交对应seq；不能用词匹配证明中奖 |
| 15. 中断/部分成功 | formal传播；昵称仅停止后续昵称补读；会话列表cap未实现独立持久追赶cursor |
| 16. 重启恢复 | 已保存seq/候选继续；未完成会话保守重读 |
| 17. 旧活动遗漏边界 | 昵称补充请求遇正式风险只停止昵称查找而吞异常，是否应全局暂停另见疑点；会话列表hard cap未持久resume。 |
| 18. 重复请求/分类 | 消息页/未完会话可能重读；通知claim防重复派送，与参与guard无关 |
| 19. 功能重叠 | 与账号消息角标读入口部分重叠；Winner独立，不走DS活动库 |
| 20. 多账号/调度 | 当前Profile Runner互斥；多Profile独立且Winner不受V116错峰，跨进程请求可能重叠 |

#### SC-016：Winner 候选通知中断恢复

| 问题 | 源码结论 |
|---|---|
| 1. 名称 | Winner 候选通知中断恢复 |
| 2. 接口/页面 | 无B站新扫描；四渠道通知HTTP是外部写副作用 |
| 3. 内容范围 | Winner候选到通知的持久交付，不是参与动作 |
| 4. 触发者 | Winner run启动先恢复；新候选dispatch |
| 5. 入口位置 | src/winner_scan/service.py::_save_new_candidates（1537）/_dispatch_prepared（1554）；src/notification/service.py |
| 6. 调用链 | 候选+preparation同一原子落盘 → 持久claim全部渠道 → 外部HTTP → terminal → source checkpoint |
| 7. 数量/分页 | 按未完成preparation/配置渠道逐项；pending候选不因500裁剪直接丢弃 |
| 8. 进度存储 | winner_candidates通知准备；notify_outbox/claims/tombstones |
| 9. 已扫描判据 | event+channel claim；历史删除隐藏保留tombstone |
| 10. 去重 | 独立消息key/event+channel；不依赖activities |
| 11. 抽奖识别 | @/回复/私信本地词匹配仅为线索；通知恢复不新判型 |
| 12. LLM条件 | 不调用LLM |
| 13. 最终数据 | winner_candidates通知准备；notify_outbox/claims/tombstones |
| 14. 成功条件 | 先建立全部claim，再派送/持久terminal，再允许提交源checkpoint |
| 15. 中断/部分成功 | dispatching前落盘；进程中断后delivery_unknown不自动重发；不能声称exactly-once送达 |
| 16. 重启恢复 | 恢复候选准备窗口，已确认/不确定claim不重新提交外部通知 |
| 17. 旧活动遗漏边界 | 这是通知恢复，不是B站参与重试；超过500候选的保留方向另列风险。 |
| 18. 重复请求/分类 | 消息页/未完会话可能重读；通知claim防重复派送，与参与guard无关 |
| 19. 功能重叠 | 与账号消息角标读入口部分重叠；Winner独立，不走DS活动库 |
| 20. 多账号/调度 | 当前Profile Runner互斥；多Profile独立且Winner不受V116错峰，跨进程请求可能重叠 |

#### SC-017：串行更新所有数据源

| 问题 | 源码结论 |
|---|---|
| 1. 名称 | 串行更新所有数据源 |
| 2. 接口/页面 | 顺序复用所有注册来源API（DS1～7及DS9） |
| 3. 内容范围 | 一键更新所有DS卡片对应的完整单源流程 |
| 4. 触发者 | 数据源‘更新全部’；Job update_all_datasources |
| 5. 入口位置 | web/actions.py::_update_all_datasources（870）；web/frontend/src/sources/update-all.ts |
| 6. 调用链 | 按当前 DS_HANDLERS 的 DS-1→2→3→4→5→6→7→9 串行检查、分类、详情、入库和条件checkpoint；完成一个源再处理下一个 |
| 7. 数量/分页 | 8源串行；每源仍有自己的数量上限；不是无限源/全历史 |
| 8. 进度存储 | 每源DB snapshot/activities/checkpoint及Job汇总 |
| 9. 已扫描判据 | 各源container/fp +活动canonical去重 |
| 10. 去重 | 原始动态ID→canonical；本轮seen、初始activities、最终fresh DB复查；Following跨批context |
| 11. 抽奖识别 | 公共classify_new_link：坏动态/充电→官方互动→非直播预约→普通转发→非抽奖 |
| 12. LLM条件 | 普通转发判型/内容结构提取可调用LLM；失败不等于非抽奖；已有可靠缓存可复用 |
| 13. 最终数据 | 每源DB snapshot/activities/checkpoint及Job汇总 |
| 14. 成功条件 | 每源完整返回后下一个；普通失败可最终partial，不等于所有源成功 |
| 15. 中断/部分成功 | 普通单源失败记结果后继续；formal/取消立即停止尾部；已保存源不丢 |
| 16. 重启恢复 | 已成功源容器可跳过；失败源保持未完成 |
| 17. 旧活动遗漏边界 | 当前注册8个来源，包括DS9、没有DS8；函数注释仍写DS1～7，不能按旧注释缩小实际范围。普通单源失败继续，正式风险/取消停止后续源。 |
| 18. 重复请求/分类 | 已存有效活动可复用；未完成批次、非抽奖和分页获取可能重做，不能称零请求重试 |
| 19. 功能重叠 | DS/Following/WATCH可能发现同一original；共享canonical去重但各自抓取仍有请求重叠 |
| 20. 多账号/调度 | 当前Profile Runner扫描优先/忙门禁；跨Profile独立；仅自动refresh/following rank×15分钟，没有全局任务锁 |

#### SC-018：自动刷新批次及内部 refresh_all

| 问题 | 源码结论 |
|---|---|
| 1. 名称 | 自动刷新批次及内部 refresh_all |
| 2. 接口/页面 | refresh_all注册来源并行检查 → 公共合并分类；refresh_watch → 本地refresh_status |
| 3. 内容范围 | Scheduler固定refresh批次，也有旧API/CLI入口 |
| 4. 触发者 | 自动refresh槽位；CLI/MCP旧refresh_all；后台 run_action |
| 5. 入口位置 | web/actions.py::run_action（1219）；web/auto_scheduler.py::REFRESH_STEPS（65）；src/pipeline/refresh_all_pipeline.py::run_refresh_all_pipeline（1062） |
| 6. 调用链 | refresh_all 检查注册来源并处理 → refresh_watch → refresh_status；阶段失败/风险依既有契约处理 |
| 7. 数量/分页 | refresh_all检查8源；Pipeline默认workers4；三阶段顺序执行；V116只batch/following错峰 |
| 8. 进度存储 | 各源snapshot/checkpoint/activities；scheduler_slots/auto_remote_state |
| 9. 已扫描判据 | 任务逻辑槽位去重+容器去重+canonical DB去重 |
| 10. 去重 | 原始动态ID→canonical；本轮seen、初始activities、最终fresh DB复查；Following跨批context |
| 11. 抽奖识别 | 公共classify_new_link：坏动态/充电→官方互动→非直播预约→普通转发→非抽奖 |
| 12. LLM条件 | 普通转发判型/内容结构提取可调用LLM；失败不等于非抽奖；已有可靠缓存可复用 |
| 13. 最终数据 | 各源snapshot/checkpoint/activities；scheduler_slots/auto_remote_state |
| 14. 成功条件 | 各阶段依契约保存，refresh_status可在远端gate阻挡时仍本地刷新 |
| 15. 中断/部分成功 | formal阻止后续远端请求；partial不假推进source完成；不抢占已有Runner Job |
| 16. 重启恢复 | slot/cp持久恢复，不即时重复已完成batch |
| 17. 旧活动遗漏边界 | 与UI串行更新全部入口范围不同；旧CLI/MCP接口需核对版本和端口。 |
| 18. 重复请求/分类 | 已存有效活动可复用；未完成批次、非抽奖和分页获取可能重做，不能称零请求重试 |
| 19. 功能重叠 | DS/Following/WATCH可能发现同一original；共享canonical去重但各自抓取仍有请求重叠 |
| 20. 多账号/调度 | 当前Profile Runner扫描优先/忙门禁；跨Profile独立；仅自动refresh/following rank×15分钟，没有全局任务锁 |


## 参与动作矩阵、15问与真实调用链

三连不是‘对每条只做三个动作’：它选择最多3个抽奖target，在同一Job中可有3个worker。互动/普通转发的顺序是点赞→关注→收藏（目标支持才做）→转发→评论；预约为关注→预约。必须区分活动数与动作数。

| 功能/动作 | 执行位置 | 实际API及参数 | 正向证据 | 持久状态 | 重试边界 |
|---|---|---|---|---|---|
| ACT-004 点赞 | src/lottery_actions.py::like_dynamic:671 | /x/dynamic/feed/dyn/thumb（POST JSON）；dyn_id_str、up=1、spmid；csrf query | 本次context.liked True或已成功日志可跳过；API code=0/65006成功 | 无专用长久pending；动作日志记录 | 默认有限客户端重试，不是retries=0 |
| ACT-005 关注 | src/lottery_actions.py::follow_user:694 | /x/relation/modify（POST form）；fid=UP UID、act=1、re_src=11、csrf | /x/relation确认当前关注；code=0/22014成功 | 无专用长久pending；动作日志记录 | 明确关注意图；客户端有限重试 |
| ACT-006 收藏 | src/lottery_actions.py::favorite_dynamic:711 | /x/community/cosmo/interface/simple_action（POST JSON）；entity.object_id_str=dynamic_id、type.biz=2、action=3；不猜comment_rid | /opus/detail可靠is_fav True跳过、False才能POST；读取不可靠不POST；code0还须复查True，65006/75008按现有契约确认 | favorite_confirmation按Profile/UID/target；pending先落盘；明确拒绝failed；未知unknown | POST retries=0；pending/unknown自动禁止；人工本次授权仍先查状态 |
| ACT-007 转发 | src/lottery_actions.py::repost_dynamic:823 | api.vc /dynamic_repost/v1/dynamic_repost/repost（POST form）；uid、dynamic_id、content[:233]、ctrl=[]、csrf | 本地已参加0目标远端；guard confirmed跳过；官方notice.reposted可正向证明；普通点赞收藏只能suspected | DB participation_guard(uid,dynamic_id)；pending→严格整数code0立即confirmed，不等评论；其余不确定unknown | POST retries=0；无个人空间实时查重；unknown/pending自动不重发 |
| ACT-008 评论 | src/lottery_actions.py::comment_dynamic:886 | /x/v2/reply/add（POST form）；oid=可靠rid、type、message=原文案生成规则、csrf | comment_confirmed本地confirmed跳过；code0/12051确认；breaker's12014阻止普通评论 | Profile comment_confirmed.json按UID+canonical；旧无UID confirmed全Profile保守阻挡；pending先落盘 | POST retries=0；有限明确拒绝failed，人工拒绝仍保留原unknown |
| ACT-009 预约 | src/participation.py::_reserve_click:774 | /x/dynamic/feed/reserve/click（POST JSON）；reserve_id、cur_btn_status、dynamic_id_str、reserve_total、csrf | 预约已reserved直接跳过；code0+final_btn_status或既有可靠成功文本 | 动作日志/整体参与；没有专用长期reserve pending表 | POST retries=0；异常unknown日志阻止当轮，裁剪后保护边界待独立评估 |
| ACT-017 恢复探测评论 | src/comment_probe.py::run_comment_recovery_probe:30；src/lottery_actions.py::comment_dynamic_probe:981 | 同/x/v2/reply/add；这是真实mutation；本机候选解析rid/type/原评论规则，不由LLM新增任务 | Runner空闲、12014 breaker、到期、安全候选；已有评论guard阻挡；恢复成功后confirm_comment | 先持久claim in_flight；崩溃恢复为probe unknown并排除该target；不是普通comment pending | retries=0；每小时可换另一个候选；probe异常处理/clear边界见D-021 |

```text
活动行“参与” → participationClickParams(dynamic_id, manual_unknown_retry=true)
  → POST /api/jobs action=participate（当前Profile/UID冻结、正式风险/Runner忙/restart gate）
  → web/actions._execute_participate → 当前类型/有效目标检查
  → participation.participate_activity
     → gate(实际runtime DB路径, UID, dynamic_id)，覆盖整个生命周期
     → 本地整体已参加：直接skip，0目标远端；其余guard/旧unknown检查
     → 本次只给可可靠归属的pending/unknown动作建立人工attempt
     → 目标preflight / 当前交互context；身份每步重新核验
     → 原有文案生成（自定义/随机评论池；不要求用户填写/口令）
     → execute_full_participation：like→follow→favorite→repost→comment
          confirmed：skip；未知读状态：不POST
          需要动作：pending/attempt先持久，retries=0（收藏/转发/评论）
          明确成功：立即confirmed；再次不明：unknown并停止后续
     → 持久动作日志/整体成功/失败backoff（整体成功与某动作confirmed不同）
     → 新确认转发标history dirty；后续手动同步本人的真实repost ID
自动Scheduler → participate_triple → 同样gate/Pipeline，manual授权=false
预约 → 原有follow→reserve分支，不获取V112人工unknown授权
CLI/MCP → 公共参与入口但不自带Web人工授权；不等同当前“参与”按钮
```

### 持久状态的实际区别

| 状态 | 事实 | 自动行为 | 当前人工入口 |
|---|---|---|---|
| pending | POST前成功持久的屏障；崩溃可能发生在发送前或后 | 不自动过期/补发 | 可靠归属的comment/favorite/repost可建立本次独立attempt；不删除旧诊断 |
| unknown | 可能已执行但无法可靠确认 | 禁止自动重发 | 当前页面主动点击参与才授权该不明动作，重用完整Pipeline并继续未执行步骤 |
| suspected | 普通转发只有点赞+收藏等不完整痕迹 | 不伪造成已参加，不自动转发 | V112不直接解锁；可信历史exact reconciliation可单向confirmed |
| confirmed | 可靠动作成功证据 | 跳过该动作POST | 继续其他未完成步骤，不能因人工授权重复已确认成功动作 |
| failed | 评论/收藏确定拒绝等现有契约；不是repost_guard的四状态之一 | 按原失败/backoff契约 | 不能用新尝试明确拒绝去反证原UNKNOWN未执行 |

repost_guard是SQLite；comment/favorite/manual attempt是Profile-local原子JSON并有进程锁；动作日志会裁剪，不能充当永久屏障。旧无UID评论confirmed保持整个Profile保守阻挡，不猜所属账号。仅可信repost_history身份字段、UID与original ID exact match可收敛pending/unknown/suspected，deleted tombstone也证明曾转发；缺失history不允许反向解锁。接口HTTP200或LLM判断都不等于成功证据。

#### ACT-004 点赞的15问

| 问题 | 源码结论 |
|---|---|
| 1. 触发入口 | 普通页面参与/手动三连/自动三连；仅目标类型与要求适用时执行 |
| 2. 动作决定者 | 原分类/notice/目标context与当前动作条件；不是用户临时输入 |
| 3. 执行前检查 | 本地参与/guard→target有效、UID、风险、任务busy、对应交互状态 |
| 4. 已参与/交互检查 | 本次context.liked True或已成功日志可跳过；API code=0/65006成功 |
| 5. 实际API | /x/dynamic/feed/dyn/thumb（POST JSON） |
| 6. 参数/文案 | dyn_id_str、up=1、spmid；csrf query；评论/转发共用resolve_participate_text_for_activity；其他无文案 |
| 7. pending时机 | 无专用长久pending；动作日志记录 |
| 8. confirmed条件 | 本次context.liked True或已成功日志可跳过；API code=0/65006成功 |
| 9. 超时/不明 | 记录动作失败/结果不明；没有同等专用长期屏障，不能泛化保证 |
| 10. 自动再次执行 | 默认有限客户端重试，不是retries=0 |
| 11. 重启保护 | 可靠保留日志/平台正向状态可复用；裁剪前未知残余不能凭空还原 |
| 12. 自动/手动差异 | 自动不获manual attempt；页面主动点击才给该目标可靠不明动作本次授权 |
| 13. 成功动作重复风险 | 使用本地日志和可靠平台状态跳过；不声称有独立永久屏障 |
| 14. 并发边界 | 整个参与gate以实际DB路径+UID+dynamic锁；评论/收藏另有动作级锁；不同Profile互不写入 |
| 15. 风控/互斥 | Runner单Job及风险gate；同Job三targets共享risk breaker；-352/-509/HTTP429才是formal |

#### ACT-005 关注的15问

| 问题 | 源码结论 |
|---|---|
| 1. 触发入口 | 普通页面参与/手动三连/自动三连；仅目标类型与要求适用时执行 |
| 2. 动作决定者 | 原分类/notice/目标context与当前动作条件；不是用户临时输入 |
| 3. 执行前检查 | 本地参与/guard→target有效、UID、风险、任务busy、对应交互状态 |
| 4. 已参与/交互检查 | /x/relation确认当前关注；code=0/22014成功 |
| 5. 实际API | /x/relation/modify（POST form） |
| 6. 参数/文案 | fid=UP UID、act=1、re_src=11、csrf；评论/转发共用resolve_participate_text_for_activity；其他无文案 |
| 7. pending时机 | 无专用长久pending；动作日志记录 |
| 8. confirmed条件 | /x/relation确认当前关注；code=0/22014成功 |
| 9. 超时/不明 | 记录动作失败/结果不明；没有同等专用长期屏障，不能泛化保证 |
| 10. 自动再次执行 | 明确关注意图；客户端有限重试 |
| 11. 重启保护 | 可靠保留日志/平台正向状态可复用；裁剪前未知残余不能凭空还原 |
| 12. 自动/手动差异 | 自动不获manual attempt；页面主动点击才给该目标可靠不明动作本次授权 |
| 13. 成功动作重复风险 | 使用本地日志和可靠平台状态跳过；不声称有独立永久屏障 |
| 14. 并发边界 | 整个参与gate以实际DB路径+UID+dynamic锁；评论/收藏另有动作级锁；不同Profile互不写入 |
| 15. 风控/互斥 | Runner单Job及风险gate；同Job三targets共享risk breaker；-352/-509/HTTP429才是formal |

#### ACT-006 收藏的15问

| 问题 | 源码结论 |
|---|---|
| 1. 触发入口 | 普通页面参与/手动三连/自动三连；仅目标类型与要求适用时执行 |
| 2. 动作决定者 | 原分类/notice/目标context与当前动作条件；不是用户临时输入 |
| 3. 执行前检查 | 本地参与/guard→target有效、UID、风险、任务busy、对应交互状态 |
| 4. 已参与/交互检查 | /opus/detail可靠is_fav True跳过、False才能POST；读取不可靠不POST；code0还须复查True，65006/75008按现有契约确认 |
| 5. 实际API | /x/community/cosmo/interface/simple_action（POST JSON） |
| 6. 参数/文案 | entity.object_id_str=dynamic_id、type.biz=2、action=3；不猜comment_rid；评论/转发共用resolve_participate_text_for_activity；其他无文案 |
| 7. pending时机 | favorite_confirmation按Profile/UID/target；pending先落盘；明确拒绝failed；未知unknown |
| 8. confirmed条件 | /opus/detail可靠is_fav True跳过、False才能POST；读取不可靠不POST；code0还须复查True，65006/75008按现有契约确认 |
| 9. 超时/不明 | 先unknown；写失败原pending仍保守阻挡；正式风险仍须传播 |
| 10. 自动再次执行 | POST retries=0；pending/unknown自动禁止；人工本次授权仍先查状态 |
| 11. 重启保护 | 永久guard不随日志裁剪；pending/unknown恢复仍阻挡 |
| 12. 自动/手动差异 | 自动不获manual attempt；页面主动点击才给该目标可靠不明动作本次授权 |
| 13. 成功动作重复风险 | 本地confirmed跳过；跨步持久成功不等评论/整体流程完成 |
| 14. 并发边界 | 整个参与gate以实际DB路径+UID+dynamic锁；评论/收藏另有动作级锁；不同Profile互不写入 |
| 15. 风控/互斥 | Runner单Job及风险gate；同Job三targets共享risk breaker；-352/-509/HTTP429才是formal |

#### ACT-007 转发的15问

| 问题 | 源码结论 |
|---|---|
| 1. 触发入口 | 普通页面参与/手动三连/自动三连；仅目标类型与要求适用时执行 |
| 2. 动作决定者 | 原分类/notice/目标context与当前动作条件；不是用户临时输入 |
| 3. 执行前检查 | 本地参与/guard→target有效、UID、风险、任务busy、对应交互状态 |
| 4. 已参与/交互检查 | 本地已参加0目标远端；guard confirmed跳过；官方notice.reposted可正向证明；普通点赞收藏只能suspected |
| 5. 实际API | api.vc /dynamic_repost/v1/dynamic_repost/repost（POST form） |
| 6. 参数/文案 | uid、dynamic_id、content[:233]、ctrl=[]、csrf；评论/转发共用resolve_participate_text_for_activity；其他无文案 |
| 7. pending时机 | DB participation_guard(uid,dynamic_id)；pending→严格整数code0立即confirmed，不等评论；其余不确定unknown |
| 8. confirmed条件 | 本地已参加0目标远端；guard confirmed跳过；官方notice.reposted可正向证明；普通点赞收藏只能suspected |
| 9. 超时/不明 | 先unknown；写失败原pending仍保守阻挡；正式风险仍须传播 |
| 10. 自动再次执行 | POST retries=0；无个人空间实时查重；unknown/pending自动不重发 |
| 11. 重启保护 | 永久guard不随日志裁剪；pending/unknown恢复仍阻挡 |
| 12. 自动/手动差异 | 自动不获manual attempt；页面主动点击才给该目标可靠不明动作本次授权 |
| 13. 成功动作重复风险 | 本地confirmed跳过；跨步持久成功不等评论/整体流程完成 |
| 14. 并发边界 | 整个参与gate以实际DB路径+UID+dynamic锁；评论/收藏另有动作级锁；不同Profile互不写入 |
| 15. 风控/互斥 | Runner单Job及风险gate；同Job三targets共享risk breaker；-352/-509/HTTP429才是formal |

#### ACT-008 评论的15问

| 问题 | 源码结论 |
|---|---|
| 1. 触发入口 | 普通页面参与/手动三连/自动三连；仅目标类型与要求适用时执行 |
| 2. 动作决定者 | 原分类/notice/目标context与当前动作条件；不是用户临时输入 |
| 3. 执行前检查 | 本地参与/guard→target有效、UID、风险、任务busy、对应交互状态 |
| 4. 已参与/交互检查 | comment_confirmed本地confirmed跳过；code0/12051确认；breaker's12014阻止普通评论 |
| 5. 实际API | /x/v2/reply/add（POST form） |
| 6. 参数/文案 | oid=可靠rid、type、message=原文案生成规则、csrf；评论/转发共用resolve_participate_text_for_activity；其他无文案 |
| 7. pending时机 | Profile comment_confirmed.json按UID+canonical；旧无UID confirmed全Profile保守阻挡；pending先落盘 |
| 8. confirmed条件 | comment_confirmed本地confirmed跳过；code0/12051确认；breaker's12014阻止普通评论 |
| 9. 超时/不明 | 先unknown；写失败原pending仍保守阻挡；正式风险仍须传播 |
| 10. 自动再次执行 | POST retries=0；有限明确拒绝failed，人工拒绝仍保留原unknown |
| 11. 重启保护 | 永久guard不随日志裁剪；pending/unknown恢复仍阻挡 |
| 12. 自动/手动差异 | 自动不获manual attempt；页面主动点击才给该目标可靠不明动作本次授权 |
| 13. 成功动作重复风险 | 本地confirmed跳过；跨步持久成功不等评论/整体流程完成 |
| 14. 并发边界 | 整个参与gate以实际DB路径+UID+dynamic锁；评论/收藏另有动作级锁；不同Profile互不写入 |
| 15. 风控/互斥 | Runner单Job及风险gate；同Job三targets共享risk breaker；-352/-509/HTTP429才是formal |

#### ACT-009 预约的15问

| 问题 | 源码结论 |
|---|---|
| 1. 触发入口 | 普通页面参与/手动三连/自动三连；仅目标类型与要求适用时执行 |
| 2. 动作决定者 | 原分类/notice/目标context与当前动作条件；不是用户临时输入 |
| 3. 执行前检查 | 本地参与/guard→target有效、UID、风险、任务busy、对应交互状态 |
| 4. 已参与/交互检查 | 预约已reserved直接跳过；code0+final_btn_status或既有可靠成功文本 |
| 5. 实际API | /x/dynamic/feed/reserve/click（POST JSON） |
| 6. 参数/文案 | reserve_id、cur_btn_status、dynamic_id_str、reserve_total、csrf；评论/转发共用resolve_participate_text_for_activity；其他无文案 |
| 7. pending时机 | 动作日志/整体参与；没有专用长期reserve pending表 |
| 8. confirmed条件 | 预约已reserved直接跳过；code0+final_btn_status或既有可靠成功文本 |
| 9. 超时/不明 | 记录动作失败/结果不明；没有同等专用长期屏障，不能泛化保证 |
| 10. 自动再次执行 | POST retries=0；异常unknown日志阻止当轮，裁剪后保护边界待独立评估 |
| 11. 重启保护 | 可靠保留日志/平台正向状态可复用；裁剪前未知残余不能凭空还原 |
| 12. 自动/手动差异 | 自动不获manual attempt；页面主动点击才给该目标可靠不明动作本次授权 |
| 13. 成功动作重复风险 | 使用本地日志和可靠平台状态跳过；不声称有独立永久屏障 |
| 14. 并发边界 | 整个参与gate以实际DB路径+UID+dynamic锁；评论/收藏另有动作级锁；不同Profile互不写入 |
| 15. 风控/互斥 | Runner单Job及风险gate；同Job三targets共享risk breaker；-352/-509/HTTP429才是formal |

#### ACT-017 恢复探测评论的15问

| 问题 | 源码结论 |
|---|---|
| 1. 触发入口 | Scheduler小时恢复探测；普通参与不直接调用此函数 |
| 2. 动作决定者 | comment breaker到期/安全候选 |
| 3. 执行前检查 | Runner空闲、remote gate、breaker claim、UID与评论锁 |
| 4. 已参与/交互检查 | Runner空闲、12014 breaker、到期、安全候选；已有评论guard阻挡；恢复成功后confirm_comment |
| 5. 实际API | 同/x/v2/reply/add；这是真实mutation |
| 6. 参数/文案 | 本机候选解析rid/type/原评论规则，不由LLM新增任务；评论/转发共用resolve_participate_text_for_activity；其他无文案 |
| 7. pending时机 | 先持久claim in_flight；崩溃恢复为probe unknown并排除该target；不是普通comment pending |
| 8. confirmed条件 | Runner空闲、12014 breaker、到期、安全候选；已有评论guard阻挡；恢复成功后confirm_comment |
| 9. 超时/不明 | probe unknown记录并排除该target；formal异常误吞与clear保留性待专项验证 |
| 10. 自动再次执行 | retries=0；每小时可换另一个候选；probe异常处理/clear边界见D-021 |
| 11. 重启保护 | claim及probe排除JSON保留，startup恢复in_flight；breaker解除后边界见D-021 |
| 12. 自动/手动差异 | 不是人工UNKNOWN授权；独立受控mutation |
| 13. 成功动作重复风险 | 使用本地日志和可靠平台状态跳过；不声称有独立永久屏障 |
| 14. 并发边界 | Profile评论mutation锁及持久claim；不通过一般参与gate |
| 15. 风控/互斥 | V101 Runner忙先阻止读/写；正式风险后的外层异常是否误吞见D-021 |

### 候选队列与人工恢复边界

`web/activity_service.py::_filtered_activity_rows:335 / _pick_triple_from_rows:442` 从当前库筛选，不要求刚扫描的新活动；旧活动是否进入取决于类型、结束/忽略/失效、整体参与、guard、可参与标志、评论暂停、失败backoff和筛选条件。显示‘未参加’不代表满足三连资格；manual_retry_available会使自动候选不可用。这是自动UNKNOWN保护，不应通过清空记录扩大队列。

当前页面统一‘参与’，没有额外确认口令或评论/转发输入；授权不是全局解除UNKNOWN。旧单动作`retry_unknown_action`仍有函数与测试，但未找到常规生产调用，不应把它的手动文案要求描述成现行UI。安全目标身份无法确认、旧不明日志缺持久可验证guard时，完整流程仍fail-closed。


## 全部执行入口索引

### FastAPI：90个显式方法/路径

88项常规路由+2项条件E2E路由；PUT/POST兼容别名单列。另有StaticFiles挂载（资产），不是新业务API。以下是源码索引，不是接口调用结果；本轮没有调用任何服务。GET也不一律无副作用，例如账号远端查询、registry陈旧清理、监控seed及诊断包导出。

`web/app.py` / `src/dashboard_server.py`限制loopback；POST/PUT/DELETE执行Origin校验，但这不是互联网用户鉴权服务。不要对公网暴露，也不能用‘localhost’证明调用的是正确Profile。

| 方法/路径 | 功能 | 源码入口 | 实际边界/副作用 |
|---|---|---|---|
| GET /api/watch-users | UI-006/SC-012 | web/app.py::api_watch_users:249 | 读取本地监控列表；首次兼容seed可写本地 |
| POST /api/watch-users | UI-006/SC-012 | web/app.py::api_add_watch_user:264 | 添加UP；可能远端账户验证 |
| DELETE /api/watch-users/{mid} | UI-006/SC-012 | web/app.py::api_remove_watch_user:278 | 移除本地UP；不是取消关注POST |
| GET /api/account | UI-013/SYS-004 | web/app.py::api_account:293 | 账号资料；缓存不足/force可B站NAV等GET，不是0远端保证 |
| GET /api/profiles | SYS-002/SYS-008 | web/app.py::api_profiles:341 | Profile列表与runtime；registry陈旧记录核验/清理可写本地；不换runtime |
| GET /api/runtime/ready | SYS-008 | web/app.py::api_runtime_ready:364 | 本机冻结Profile/instance/port身份，无业务Job |
| GET /api/runtime/tray-health | SYS-012 | web/app.py::api_runtime_tray_health:377 | 当前Profile轻量Scheduler/Runner/risk内存快照；不B站/LLM/历史DB |
| POST /api/profiles/{profile_id}/launch | SYS-007 | web/app.py::api_launch_profile:392 | 冻结目标Profile、端口、instance reservation与单实例锁后新supervisor |
| POST /api/profiles | SYS-002/SYS-008 | web/app.py::api_create_profile:418 | 建立新Profile与独立本地数据 |
| POST /api/profiles/{profile_id}/activate | SYS-005/JOB-009 | web/app.py::api_activate_profile:430 | 兼容active选择接口；不能把active当当前runtime |
| POST /api/profiles/{profile_id}/switch | SYS-005/JOB-009 | web/app.py::api_switch_profile:457 | 空闲+restart gate→active pointer→退出75→新进程；不热切换 |
| DELETE /api/profiles/{profile_id} | SYS-006 | web/app.py::api_delete_profile:531 | 本地删除Profile；active/current/running禁止；不是B站删除 |
| GET /api/account/extras | UI-013/NTF-005 | web/app.py::api_account_extras:546 | 余额/消息角标；可B站GET，10秒缓存 |
| POST /api/account/ack-at-unread | NTF-005 | web/app.py::api_ack_at_unread:554 | 仅本地记录角标已看；不是Winner平台mark_read |
| GET /api/summary | UI-004 | web/app.py::api_summary:564 | 当前Profile本地活动汇总 |
| GET /api/ds9 | UI-007/SC-008 | web/app.py::api_ds9_status:571 | 话题卡片状态、独立checkpoint |
| GET /api/ds9/rejection-audit | UI-008 | web/app.py::api_ds9_rejection_audit:587 | 读取/CSV导出隐藏非抽奖审计样本 |
| GET /api/ds9/rejection-audit.csv | UI-008 | web/app.py::api_ds9_rejection_audit_csv:594 | 读取/CSV导出隐藏非抽奖审计样本 |
| DELETE /api/ds9/rejection-audit | UI-008 | web/app.py::api_ds9_clear_rejection_audit:605 | 清本地审计样本，非活动/guard/平台删除 |
| GET /api/ds9/contribution | UI-008 | web/app.py::api_ds9_contribution:611 | 本地贡献/独有贡献统计与CSV导出 |
| GET /api/ds9/contribution/unique | UI-008 | web/app.py::api_ds9_unique_contribution:618 | 本地贡献/独有贡献统计与CSV导出 |
| GET /api/ds9/contribution.csv | UI-008 | web/app.py::api_ds9_contribution_csv:624 | 本地贡献/独有贡献统计与CSV导出 |
| POST /api/ds9/topics | UI-007 | web/app.py::api_ds9_add_topic:643 | 校验并添加话题，本地配置；可能远端验证 |
| DELETE /api/ds9/topics/{topic_id} | UI-007 | web/app.py::api_ds9_remove_topic:652 | 移除本地话题，不删已入库活动 |
| PUT /api/ds9/topics/{topic_id}/enabled | UI-007 | web/app.py::api_ds9_set_topic_enabled:663 | 启停话题，不清已有活动 |
| GET /api/notifications | NTF-001/003 | web/app.py::api_notifications_status:691 | 脱敏配置与本地派送记录 |
| DELETE /api/notifications/records/{event_id}/{channel} | NTF-003 | web/app.py::api_notifications_delete_record:698 | 隐藏本地通知记录，保留claim/tombstone |
| POST /api/notifications/channels/{channel}/config | NTF-001 | web/app.py::api_notifications_save_channel:712 | 脱敏渠道配置/启停本地保存 |
| POST /api/notifications/channels/{channel}/enabled | NTF-001 | web/app.py::api_notifications_set_enabled:722 | 脱敏渠道配置/启停本地保存 |
| POST /api/notifications/events/{event}/enabled | NTF-006 | web/app.py::api_notifications_set_event:731 | 保存开关；预留事件不意味着有生产者 |
| POST /api/notifications/channels/{channel}/test | NTF-002 | web/app.py::api_notifications_test:740 | 显式通知渠道外部HTTP测试 |
| GET /api/cleanup/auto-delete | CLN-008 | web/app.py::api_auto_delete_status:756 | 纯手动连续清理开关/状态；名称为兼容 |
| PUT /api/cleanup/auto-delete | CLN-008 | web/app.py::api_auto_delete_set:789 | 保存开关，不能单独启动定时DELETE |
| POST /api/accounts/onboarding/start | SYS-003 | web/app.py::api_onboarding_start:820 | 新账号暂存QR流程，可登录平台读取；不替换运行Profile Cookie |
| GET /api/accounts/onboarding/{onboarding_id}/qr | SYS-003 | web/app.py::api_onboarding_qr:831 | 已有onboarding暂存状态/二维码只读 |
| GET /api/accounts/onboarding/{onboarding_id}/status | SYS-003 | web/app.py::api_onboarding_status:845 | 已有onboarding暂存状态/二维码只读 |
| POST /api/accounts/onboarding/{onboarding_id}/poll | SYS-003 | web/app.py::api_onboarding_poll:855 | 新账号暂存QR流程，可登录平台读取；不替换运行Profile Cookie |
| POST /api/accounts/onboarding/complete | SYS-003 | web/app.py::api_onboarding_complete:868 | 验证UID/receipt、幂等登记新Profile；拒绝覆盖运行账号 |
| GET /api/winner-scan | SC-014/NTF-004 | web/app.py::api_winner_scan_status:885 | 独立候选/游标读；不发扫描 |
| POST /api/winner-scan/candidates/{candidate_key}/suppress | NTF-004 | web/app.py::api_suppress_winner_candidate:906 | 本地suppress/delete候选；不改平台消息/自动参与 |
| DELETE /api/winner-scan/candidates/{candidate_key} | NTF-004 | web/app.py::api_delete_winner_candidate:911 | 本地suppress/delete候选；不改平台消息/自动参与 |
| GET /api/activities | UI-001 | web/app.py::api_activities:920 | 本地列表/搜索/筛选/排序/分页与保护展示 |
| GET /api/activity-failures | UI-003 | web/app.py::api_activity_failures:945 | 本地失败记录 |
| DELETE /api/activity-failures/{dynamic_id} | UI-003 | web/app.py::api_delete_activity_failure:952 | 本地失败记录删除，deleted:false不成功；不清guard |
| DELETE /api/activities/{dynamic_id} | UI-002 | web/app.py::api_delete_activity:958 | 人工忽略/本地移除活动；不是remote DELETE，不允许删除guard来重发 |
| GET /api/activities/triple-targets | ACT-002 | web/app.py::api_triple_participate_targets:972 | 本地资格预览；不启动参与、不清guard |
| GET /api/participation/comment-breaker | ACT-017 | web/app.py::api_comment_breaker:993 | 评论暂停/恢复计划状态；GET不发送探测 |
| GET /api/repost-cleanup/history | CLN-001/005 | web/app.py::api_repost_cleanup_history:1023 | 本地本人转发账本分页 |
| GET /api/repost-cleanup/candidates | CLN-003/005 | web/app.py::api_repost_cleanup_candidates:1058 | 本地评估候选；不即刻重查平台 |
| GET /api/repost-cleanup/health | CLN-010 | web/app.py::api_repost_cleanup_health:1074 | 本地UNKNOWN/pending/dirty/cursor健康诊断 |
| POST /api/repost-cleanup/defer | CLN-006 | web/app.py::api_repost_cleanup_defer:1123 | 仅人工暂缓本地记录 |
| POST /api/repost-cleanup/restore | CLN-006 | web/app.py::api_repost_cleanup_restore:1129 | 仅恢复暂缓处理；不解锁DELETE unknown |
| POST /api/repost-cleanup/delete | CLN-007 | web/app.py::api_delete_expired_reposts:1139 | 专用人工删除Job，选repost IDs与二次确认；严格重新验证后平台remove，retries0 |
| POST /api/jobs | JOB-006/ACT-001 | web/app.py::api_start_job:1172 | 只白名单动作；restart、身份、busy、正式风险门禁；可能远端读写，不能拿来只读验证 |
| GET /api/participation-unknown/{dynamic_id} | ACT-013 | web/app.py::api_participation_unknown:1223 | 本地可靠归属动作/恢复原因，不释放UNKNOWN，不发参与 |
| POST /api/jobs/cancel | JOB-002 | web/app.py::api_cancel_job:1234 | 设置当前Job独立cancel事件；不是强杀进程 |
| GET /api/jobs/current | JOB-001/UI-009 | web/app.py::api_current_job:1241 | 当前Job只读快照 |
| GET /api/runtime | UI-016/SYS-001 | web/app.py::api_runtime:1246 | 运行参数/路径状态与健康信息 |
| POST /api/updates/check | UI-016 | web/app.py::api_updates_check:1260 | 显式查GitHub更新；非B站，UI常规按钮调用未确认 |
| GET /api/diagnostics/logs | UI-015/SYS-020 | web/app.py::api_diagnostics_logs:1273 | 只读过滤本地日志/Job摘要 |
| GET /api/diagnostics/bundle | UI-015 | web/app.py::api_diagnostics_bundle:1297 | GET创建/导出本地脱敏诊断包，有本地文件副作用 |
| GET /api/events | JOB-008 | web/app.py::api_events:1309 | 单一当前Job/Scheduler事件SSE；非多Job并行显示 |
| GET /api/jobs/{job_id} | JOB-006/OPS-023 | web/app.py::api_job_by_id:1322 | 只读指定Job ID，不用当前Job错认替换任务 |
| GET /api/auto/status | JOB-007 | web/app.py::api_auto_status:1334 | 当前真实计划；next_task最近所有任务，不专指扫描 |
| GET /api/auto/night-continue | JOB-004 | web/app.py::api_auto_night_continue_status:1353 | 本地设置 |
| PUT /api/auto/night-continue | JOB-004 | web/app.py::api_auto_night_continue_toggle:1360 | 本地夜间继续开关，不绕过risk |
| POST /api/auto/night-continue | JOB-004 | web/app.py::api_auto_night_continue_toggle:1360 | 本地夜间继续开关，不绕过risk |
| POST /api/auto/start | JOB-007 | web/app.py::api_auto_start:1369 | 开启调度，后续可启动真实扫描/参与 |
| POST /api/auto/stop | JOB-007 | web/app.py::api_auto_stop:1382 | 停止未来调度；按Runner规则处理当前Job，不等同进程关机 |
| GET /api/auto/following-feed | SC-009/JOB-007 | web/app.py::api_auto_following_feed_status:1388 | Following调度只读状态 |
| GET /api/auto/cleanup-maintain | CLN-010 | web/app.py::api_auto_cleanup_maintain_status:1400 | 兼容只读，manual-only，不生产定时清理 |
| POST /api/auto/cleanup-maintain | CLN-010 | web/app.py::api_auto_cleanup_maintain_toggle:1416 | 兼容配置端点，不让Scheduler自动执行cleanup |
| POST /api/auto/cleanup-maintain/recover | CLN-010 | web/app.py::api_auto_cleanup_maintain_recover:1431 | 遗留恢复契约；不解除不确定删除 |
| GET /api/settings | UI-014/ACT-010 | web/app.py::api_settings:1457 | 本地偏好/配置状态 |
| GET /api/settings/llm | UI-014/SYS-027 | web/app.py::api_get_llm_settings:1462 | 脱敏LLM配置 |
| POST /api/settings/llm/test | UI-014 | web/app.py::api_test_llm_settings:1473 | 显式外部LLM请求，改变ready fingerprint |
| POST /api/settings/llm | UI-014/SYS-027 | web/app.py::api_update_llm_settings:1510 | 保存shared/llm.env；不等于连接验证 |
| PUT /api/settings/llm | UI-014/SYS-027 | web/app.py::api_update_llm_settings:1510 | 保存shared/llm.env；不等于连接验证 |
| PUT /api/settings/participate-text | ACT-010 | web/app.py::api_update_participate_text:1532 | 保存UID自定义文案，无参与POST |
| POST /api/settings/participate-text | ACT-010 | web/app.py::api_update_participate_text:1532 | 保存UID自定义文案，无参与POST |
| PUT /api/settings/participate-text-mode | ACT-010 | web/app.py::api_update_participate_text_mode:1554 | 保存自定义/随机借用评论模式 |
| POST /api/settings/participate-text-mode | ACT-010 | web/app.py::api_update_participate_text_mode:1554 | 保存自定义/随机借用评论模式 |
| POST /api/logout | SYS-004 | web/app.py::api_logout:1567 | 本Profile退出登录/本地Cookie处理；非进程退出 |
| GET /api/login/qrcode | SYS-004/UI-012 | web/app.py::api_login_qrcode:1576 | 已有登录Job生成的二维码文件读；不创建Job |
| GET /app.js | SYS-021 | web/app.py::api_legacy_static_gone:1591 | 旧静态资源明确410 Gone |
| GET /styles.css | SYS-021 | web/app.py::api_legacy_static_gone:1591 | 旧静态资源明确410 Gone |
| GET /favicon.svg | SYS-021 | web/app.py::api_favicon:1600 | 正式图标静态文件 |
| GET / | SYS-021 | web/app.py::spa_index:1617 | Vite构建SPA index；不是Vue |
| GET /api/testing/e2e-state | SYS-022 | web/e2e_hooks.py::api_e2e_state_get:153 | 仅E2E配置启用；GET读fixture，POST写fixture，不是正式生产接口 |
| POST /api/testing/e2e-state | SYS-022 | web/e2e_hooks.py::api_e2e_state_set:158 | 仅E2E配置启用；GET读fixture，POST写fixture，不是正式生产接口 |

### Job白名单、内部任务与后台周期

`web/schemas/jobs.py::ALLOWED_JOB_ACTIONS:7`只有以下16项。writer lease的action集合不是HTTP Job白名单；`delete_expired_reposts`由专用清理POST创建，`cleanup_maintenance_cycle`只有内部run_action分支且常规生产入口未确认，不能称可任意POST /api/jobs运行。

| Job action | 功能 | 条件/作用 |
|---|---|---|
| login | SYS-004 | 二维码登录，可写当前Profile Cookie |
| refresh_all | SC-018 | 注册8个来源检查并行→合并公共Pipeline |
| refresh_source | SC-001～008 | 仅指定source完整Pipeline；DS9允许 |
| refresh_watch | SC-012/013 | 指定UP转发抓取+公共Pipeline |
| refresh_status | SC-023 | 本地状态/提醒刷新，不调用中奖扫描 |
| update_all_datasources | SC-017 | 注册8源串行，不是旧注释7源 |
| following_feed_scan | SC-009～011 | 最新/recent/history与25条批次 |
| winner_deep_scan | SC-014～016 | 独立消息线索，不是活动分类 |
| participate | ACT-001 | 完整Pipeline；ui本次UNKNOWN授权需冻结UID/Profile |
| manual_retry_unknown | ACT-013 | 兼容Job名，但当前同完整恢复入口，非旧retry_unknown_action |
| participate_triple | ACT-002/003 | 最多3活动；无人工unknown全局授权 |
| cleanup_auto_maintain | CLN-002/010 | 兼容名字，人工维护，不按周期触发 |
| scan_expired_reposts | CLN-003 | 人工评估；不等于自动删除 |
| cleanup_auto_delete | CLN-008 | 人工触发连续safe清理，旧auto名非Scheduler |
| sync_repost_history | CLN-001/002/ACT-015 | 手动同步真实转发，成功后本地guard reconciliation |
| sync_assess_repost_history | CLN-001/004 | 手动同步到自然完成+评估有限当轮积压 |

| 后台入口 | 实际计划/条件 | 调用与状态边界 |
|---|---|---|
| Scheduler refresh | 07:10/12:40/18:10/22:40 + 原抖动 + rank×15分钟 | REFRESH_STEPS: refresh_all→refresh_watch→refresh_status；已完成logical slot不重复 |
| Scheduler following | 08/11/14/17/20/23点 + 原抖动 + rank×15分钟 | following_feed_scan；Runner忙不抢占扫描；专用游标 |
| Scheduler winner | 12:10/22:10 + 原抖动，不做多Profile错峰 | winner_deep_scan；独立状态 |
| Scheduler participate | 上一轮完成后240～420秒；默认00～07静默顺延 | Runner接受后才running；扫描优先，不抢占；无UNKNOWN人工授权 |
| comment recovery probe | 12014暂停约1小时后；Runner空闲、remote gate先检查 | run_comment_recovery_probe；实际评论POST、持久claim；不是只读健康检查 |
| 正式风险冷却 | -352/-509/HTTP429；常量20×60秒 | auto_remote_state保守状态；源码旧注释6小时与当前值不符，以值为准 |
| supervisor/tray检查 | 父进程监督serve，托盘异常有限3次恢复；health约60秒 | 文字失败不等于图标线程死；stdin/EOF安全退出；只75重启 |
| SSE/Job进度 | 事件seq+REST权威快照；当前Job唯一 | 各Job独立cancel事件；新任务不替换共享取消token |
| 通知恢复 | Winner启动恢复保存的preparation/claim | 不自动重发delivery_unknown；不是定时发送所有预留事件 |
| logging retention | 写日志时低频检查，7天保留/每日清理一次 | flush失败/重试不等同retention频率；不执行扫描 |
| cleanup | 无Scheduler节拍 | 所有维护/评估/DELETE人工触发；兼容配置名不改变性质 |
| CI定时/事件 | .github/workflows显式push/PR/tag/schedule | 执行者是GitHub，不是当前Profile Scheduler；本轮未执行 |

`next_task`显示所有自动任务中最近一项，包括参与/Winner；托盘`next_scan`排除参与，但包括Winner且不偏移Winner。15分钟是相对各自base计划的rank偏移，不保证不同base的两个实例永远不重叠，也不阻止持续超过15分钟的跨Profile扫描。

### MCP：34个工具

`mcp/binggo_mcp/server.py`是stdio入口，工具经serial锁与`BinggoClient`访问默认localhost8787；没有完整多Profile端口选择。任务等待最长1小时，QR准备等待90秒；等待超时不证明Job结束。未暴露清理DELETE/新窗口Profile/当前Web人工UNKNOWN授权的同等入口。

| 工具 | 功能 | 实际调用 | 入口行号 |
|---|---|---|---|
| account_get | OPS-019/UI-013 | GET /api/account（可远端账号GET） | mcp/binggo_mcp/server.py::account_get:50 |
| summary_get | OPS-019 | GET /api/summary | mcp/binggo_mcp/server.py::summary_get:72 |
| settings_get | OPS-019 | GET /api/settings | mcp/binggo_mcp/server.py::settings_get:85 |
| llm_settings_get | OPS-019 | GET /api/settings/llm | mcp/binggo_mcp/server.py::llm_settings_get:98 |
| runtime_get | OPS-019 | GET /api/runtime | mcp/binggo_mcp/server.py::runtime_get:111 |
| watch_users_list | OPS-019 | GET /api/watch-users（seed边界） | mcp/binggo_mcp/server.py::watch_users_list:124 |
| activities_list | OPS-019 | GET /api/activities，原筛选分页 | mcp/binggo_mcp/server.py::activities_list:137 |
| triple_targets_get | OPS-019/ACT-002 | GET /api/activities/triple-targets | mcp/binggo_mcp/server.py::triple_targets_get:176 |
| job_get | OPS-019 | GET /api/jobs/current | mcp/binggo_mcp/server.py::job_get:210 |
| job_logs_get | OPS-019 | GET /api/diagnostics/logs | mcp/binggo_mcp/server.py::job_logs_get:224 |
| auto_status_get | OPS-019 | GET /api/auto/status | mcp/binggo_mcp/server.py::auto_status_get:242 |
| account_login_qrcode | OPS-020 | GET /api/login/qrcode；读取已有QR | mcp/binggo_mcp/server.py::account_login_qrcode:255 |
| account_login | OPS-020 | POST jobs.login，等待QR与终态 | mcp/binggo_mcp/server.py::account_login:283 |
| account_login_cancel | OPS-020 | POST /api/jobs/cancel，校验只取消login | mcp/binggo_mcp/server.py::account_login_cancel:309 |
| account_refresh | OPS-020 | force账号刷新，可能B站GET | mcp/binggo_mcp/server.py::account_refresh:322 |
| account_logout | OPS-020 | POST /api/logout，本地Cookie | mcp/binggo_mcp/server.py::account_logout:343 |
| account_ack_at_unread | OPS-020 | POST /api/account/ack-at-unread，本地ack | mcp/binggo_mcp/server.py::account_ack_at_unread:356 |
| job_refresh_watch | OPS-021 | jobs.refresh_watch | mcp/binggo_mcp/server.py::job_refresh_watch:377 |
| job_refresh_status | OPS-021 | jobs.refresh_status | mcp/binggo_mcp/server.py::job_refresh_status:390 |
| job_refresh_all | OPS-021 | jobs.refresh_all | mcp/binggo_mcp/server.py::job_refresh_all:403 |
| job_refresh_source | OPS-021 | jobs.refresh_source(source_id) | mcp/binggo_mcp/server.py::job_refresh_source:417 |
| job_participate | OPS-021 | jobs.participate(dynamic_id)，无Web manual_unknown_retry参数 | mcp/binggo_mcp/server.py::job_participate:436 |
| job_participate_triple | OPS-021 | jobs.participate_triple，原筛选 | mcp/binggo_mcp/server.py::job_participate_triple:455 |
| auto_start | OPS-021 | POST /api/auto/start | mcp/binggo_mcp/server.py::auto_start:471 |
| auto_stop | OPS-021 | POST /api/auto/stop | mcp/binggo_mcp/server.py::auto_stop:484 |
| participate_text_save | OPS-022 | 保存UID文案 | mcp/binggo_mcp/server.py::participate_text_save:494 |
| participate_text_reset | OPS-022 | 恢复默认文案 | mcp/binggo_mcp/server.py::participate_text_reset:522 |
| participate_text_mode_set | OPS-022 | 保存custom/random模式 | mcp/binggo_mcp/server.py::participate_text_mode_set:552 |
| llm_settings_save | OPS-022 | 保存LLM配置（敏感值不回显） | mcp/binggo_mcp/server.py::llm_settings_save:573 |
| llm_settings_test | OPS-022 | POST /api/settings/llm/test，外部LLM | mcp/binggo_mcp/server.py::llm_settings_test:600 |
| updates_check | OPS-022 | POST /api/updates/check，外部GitHub | mcp/binggo_mcp/server.py::updates_check:626 |
| diagnostics_export | OPS-022 | GET bundle，写本地ZIP | mcp/binggo_mcp/server.py::diagnostics_export:639 |
| watch_user_add | OPS-022 | POST watch-users，可远端校验 | mcp/binggo_mcp/server.py::watch_user_add:657 |
| watch_user_remove | OPS-022 | DELETE watch-users，本地监控列表 | mcp/binggo_mcp/server.py::watch_user_remove:672 |

### HTML静态控件：76项

来源`web/frontend/index.html`；以下按源码行列出控件与实际绑定模块（绑定模块路径均在web/frontend/src下）。SVG、文本标签、display div不计按钮；动态模板在下一表，不能用静态HTML缺按钮断言后端无入口。

| 行 | 控件/标识/文字 | 功能 | 绑定模块 | 动作边界 |
|---|---|---|---|---|
| 31 | button job-result-close；× | UI-009/ACT-001 | jobs/index.ts::hideParticipationResult | 关闭结果横幅，不取消Job |
| 43 | button qrcode-close；× | UI-012 | shell/nav.ts::bindNavigation | 隐藏QR；登录cancel行为需依登录状态，不是退出后台 |
| 65 | button app-confirm-secondary | UI-012 | shell/confirm.ts::openAppConfirm | 二次确认/取消；取消不提交目标业务 |
| 66 | button app-confirm-cancel；取消 | UI-012 | shell/confirm.ts::openAppConfirm | 二次确认/取消；取消不提交目标业务 |
| 67 | button app-confirm-yes；确认 | UI-012 | shell/confirm.ts::openAppConfirm | 二次确认/取消；取消不提交目标业务 |
| 73 | button auto-dock-toggle；⏱定时点击—运行中 | UI-010/JOB-007 | auto/index.ts | 调度坞展开/收起、night_continue、auto/start/stop；开调度会有后续真实任务 |
| 86 | button auto-dock-collapse；收起 | UI-010/JOB-007 | auto/index.ts | 调度坞展开/收起、night_continue、auto/start/stop；开调度会有后续真实任务 |
| 112 | input auto-night-continue | UI-010/JOB-007 | auto/index.ts | 调度坞展开/收起、night_continue、auto/start/stop；开调度会有后续真实任务 |
| 118 | button auto-dock-start；启动调度 | UI-010/JOB-007 | auto/index.ts | 调度坞展开/收起、night_continue、auto/start/stop；开调度会有后续真实任务 |
| 119 | button auto-dock-stop；停止调度 | UI-010/JOB-007 | auto/index.ts | 调度坞展开/收起、night_continue、auto/start/stop；开调度会有后续真实任务 |
| 131 | button log-dock-toggle；任务日志运行中 | UI-009 | shell/nav.ts::bindNavigation；jobs/index.ts::toggleLogDock | 仅日志展开/收起；不清内容/停止任务 |
| 141 | button log-dock-panel-toggle | UI-009 | shell/nav.ts::bindNavigation；jobs/index.ts::toggleLogDock | 仅日志展开/收起；不清内容/停止任务 |
| 180 | button data-section=overview；概览 | UI-011 | shell/nav.ts::switchSection | 导航并加载所属本地/远端状态接口，不创建参与Job |
| 186 | button data-section=sources；数据源 | UI-011 | shell/nav.ts::switchSection | 导航并加载所属本地/远端状态接口，不创建参与Job |
| 192 | button data-section=activities；活动 | UI-011 | shell/nav.ts::switchSection | 导航并加载所属本地/远端状态接口，不创建参与Job |
| 198 | button data-section=notifications；中奖通知 | UI-011 | shell/nav.ts::switchSection | 导航并加载所属本地/远端状态接口，不创建参与Job |
| 204 | button data-section=repost-cleanup；转发清理 | UI-011 | shell/nav.ts::switchSection | 导航并加载所属本地/远端状态接口，不创建参与Job |
| 218 | button sidebar-login；扫码登录 | SYS-004 | jobs/index.ts::bindActionButtons | jobs.login；平台QR登录与当前Cookie更新 |
| 219 | button sidebar-logout；退出登录 | SYS-004 | account/index.ts | POST logout仅账号登录，不是程序退出 |
| 220 | button sidebar-refresh-account；刷新账号 | UI-013 | account/index.ts | force账号GET可访问B站 |
| 227 | button sidebar-collapse；靠边收起 | UI-011 | shell/theme.ts / shell/nav.ts | localStorage/DOM主题及导航 |
| 233 | button theme-toggle；夜间模式 | UI-011 | shell/theme.ts / shell/nav.ts | localStorage/DOM主题及导航 |
| 248 | button sidebar-toggle；☰ | UI-011 | shell/theme.ts / shell/nav.ts | localStorage/DOM主题及导航 |
| 266 | button onboarding-skip；跳过引导 | SYS-003/UI-013 | account/index.ts；profiles/index.ts | 引导或新账号QR，须核验UID与暂存receipt；不是当前Profile热切换 |
| 277 | button onboarding-primary；下一步 | SYS-003/UI-013 | account/index.ts；profiles/index.ts | 引导或新账号QR，须核验UID与暂存receipt；不是当前Profile热切换 |
| 292 | button profile-onboarding-start；扫码登录新账号 | SYS-003/UI-013 | account/index.ts；profiles/index.ts | 引导或新账号QR，须核验UID与暂存receipt；不是当前Profile热切换 |
| 308 | button reset-participate-text；恢复默认 | ACT-010 | settings/index.ts | 自定义/随机模式、恢复默认、保存UID文案，不执行参与 |
| 312 | button data-participate-mode=custom；自定义文案 | ACT-010 | settings/index.ts | 自定义/随机模式、恢复默认、保存UID文案，不执行参与 |
| 313 | button data-participate-mode=random_comment；随机借用评论 | ACT-010 | settings/index.ts | 自定义/随机模式、恢复默认、保存UID文案，不执行参与 |
| 321 | textarea participate-text-input | ACT-010 | settings/index.ts | 自定义/随机模式、恢复默认、保存UID文案，不执行参与 |
| 325 | button save-participate-text；保存文案 | ACT-010 | settings/index.ts | 自定义/随机模式、恢复默认、保存UID文案，不执行参与 |
| 345 | input llm-api-key-input | UI-014 | settings/index.ts | LLM配置保存/刷新/显式测试；test会外部LLM请求；显示key仅UI行为 |
| 346 | button llm-api-key-toggle；显示 | UI-014 | settings/index.ts | LLM配置保存/刷新/显式测试；test会外部LLM请求；显示key仅UI行为 |
| 357 | input llm-base-url-input | UI-014 | settings/index.ts | LLM配置保存/刷新/显式测试；test会外部LLM请求；显示key仅UI行为 |
| 366 | input llm-model-name-input | UI-014 | settings/index.ts | LLM配置保存/刷新/显式测试；test会外部LLM请求；显示key仅UI行为 |
| 376 | button refresh-llm-settings；刷新配置 | UI-014 | settings/index.ts | LLM配置保存/刷新/显式测试；test会外部LLM请求；显示key仅UI行为 |
| 377 | button test-llm-settings；测试连接 | UI-014 | settings/index.ts | LLM配置保存/刷新/显式测试；test会外部LLM请求；显示key仅UI行为 |
| 378 | button save-llm-settings；保存配置 | UI-014 | settings/index.ts | LLM配置保存/刷新/显式测试；test会外部LLM请求；显示key仅UI行为 |
| 389 | button export-diagnostics；导出诊断包 | UI-015 | diagnostics/index.ts | 诊断GET可生成本地ZIP；不包含真实凭据 |
| 405 | button data-action=refresh_watch；更新监控用户动态 | SC-012/013 | jobs/index.ts::bindActionButtons | jobs.refresh_watch，真实远端读取/可能分类LLM |
| 406 | button data-action=following_feed_scan；立即扫描关注动态 | SC-009～011 | jobs/index.ts::bindActionButtons | jobs.following_feed_scan；保持25条落盘与双游标 |
| 448 | input watch-add-mid | UI-006 | watch/index.ts::submitWatchUser | 本地监控列表添加，可远端校验UP |
| 452 | button watch-add-btn；添加用户 | UI-006 | watch/index.ts::submitWatchUser | 本地监控列表添加，可远端校验UP |
| 470 | button update-all-sources-btn；一键更新全部数据源（串行） | SC-017 | sources/update-all.ts | jobs.update_all_datasources/取消；实际DS1～7及9 |
| 471 | button update-all-stop-btn；停止更新 | SC-017 | sources/update-all.ts | jobs.update_all_datasources/取消；实际DS1～7及9 |
| 483 | button data-action=refresh_status；刷新任务状态 | SC-023 | jobs/index.ts::bindActionButtons | jobs.refresh_status本地状态，不确认中奖 |
| 492 | button triple-participate-btn；三连参与 | ACT-002 | jobs/index.ts::bindActionButtons | jobs.participate_triple传当前列表筛选；最多3目标 |
| 503 | input filter-q | UI-001/ACT-002 | activities/index.ts | 仅筛选排序/搜索/提醒；更改三连预览资格 |
| 506 | button data-filter-type=；全部类型 | UI-001/ACT-002 | activities/index.ts | 仅筛选排序/搜索/提醒；更改三连预览资格 |
| 507 | button data-filter-type=互动抽奖；互动抽奖 | UI-001/ACT-002 | activities/index.ts | 仅筛选排序/搜索/提醒；更改三连预览资格 |
| 508 | button data-filter-type=转发抽奖；转发抽奖 | UI-001/ACT-002 | activities/index.ts | 仅筛选排序/搜索/提醒；更改三连预览资格 |
| 509 | button data-filter-type=预约抽奖；预约抽奖 | UI-001/ACT-002 | activities/index.ts | 仅筛选排序/搜索/提醒；更改三连预览资格 |
| 512 | button data-filter-status=；全部状态 | UI-001/ACT-002 | activities/index.ts | 仅筛选排序/搜索/提醒；更改三连预览资格 |
| 513 | button data-filter-status=未参加；未参加 | UI-001/ACT-002 | activities/index.ts | 仅筛选排序/搜索/提醒；更改三连预览资格 |
| 514 | button data-filter-status=已参加；已参加 | UI-001/ACT-002 | activities/index.ts | 仅筛选排序/搜索/提醒；更改三连预览资格 |
| 515 | button data-filter-status=已结束；已结束 | UI-001/ACT-002 | activities/index.ts | 仅筛选排序/搜索/提醒；更改三连预览资格 |
| 516 | button data-filter-status=失败；失败 | UI-001/ACT-002 | activities/index.ts | 仅筛选排序/搜索/提醒；更改三连预览资格 |
| 519 | button data-filter-sort=heat,data-filter-order=desc；热度 ↓ | UI-001/ACT-002 | activities/index.ts | 仅筛选排序/搜索/提醒；更改三连预览资格 |
| 520 | button data-filter-sort=heat,data-filter-order=asc；热度 ↑ | UI-001/ACT-002 | activities/index.ts | 仅筛选排序/搜索/提醒；更改三连预览资格 |
| 521 | button data-filter-sort=,data-filter-order=；默认排序 | UI-001/ACT-002 | activities/index.ts | 仅筛选排序/搜索/提醒；更改三连预览资格 |
| 524 | button data-filter-draw-window=soon；即将开奖 | UI-001/ACT-002 | activities/index.ts | 仅筛选排序/搜索/提醒；更改三连预览资格 |
| 571 | button repost-sync-assess-btn；同步/评估转发历史 | CLN-004/008 | repost-cleanup/index.ts | 人工同步/评估、手动连续safe删除、开关、取消；不存在自动周期DELETE |
| 572 | button auto-delete-run；立即执行安全清理 | CLN-004/008 | repost-cleanup/index.ts | 人工同步/评估、手动连续safe删除、开关、取消；不存在自动周期DELETE |
| 573 | input auto-delete-toggle | CLN-004/008 | repost-cleanup/index.ts | 人工同步/评估、手动连续safe删除、开关、取消；不存在自动周期DELETE |
| 574 | button repost-stop-btn；停止评估 | CLN-004/008 | repost-cleanup/index.ts | 人工同步/评估、手动连续safe删除、开关、取消；不存在自动周期DELETE |
| 644 | button repost-select-page；选择当前筛选结果 | CLN-005/007 | repost-cleanup/index.ts | 人工选择当前筛选/清选择/选中删除；必须二次确认本人repost ID |
| 645 | button repost-clear-selection；取消选择 | CLN-005/007 | repost-cleanup/index.ts | 人工选择当前筛选/清选择/选中删除；必须二次确认本人repost ID |
| 646 | button repost-delete-selected；删除选中的 0 条 | CLN-005/007 | repost-cleanup/index.ts | 人工选择当前筛选/清选择/选中删除；必须二次确认本人repost ID |
| 650 | button data-repost-filter=all；全部待处理 | CLN-005 | repost-cleanup/index.ts | 本地清理列表筛选/搜索/排序，不自动重评估 |
| 651 | button data-repost-filter=safe；🟢 安全可删 | CLN-005 | repost-cleanup/index.ts | 本地清理列表筛选/搜索/排序，不自动重评估 |
| 652 | button data-repost-filter=manual_review；🟡 人工确认 | CLN-005 | repost-cleanup/index.ts | 本地清理列表筛选/搜索/排序，不自动重评估 |
| 653 | button data-repost-filter=deferred；🕒 暂缓处理 | CLN-005 | repost-cleanup/index.ts | 本地清理列表筛选/搜索/排序，不自动重评估 |
| 654 | button data-repost-filter=blocked；🔴 不可判断 | CLN-005 | repost-cleanup/index.ts | 本地清理列表筛选/搜索/排序，不自动重评估 |
| 655 | button data-repost-filter=deleted；已删除 | CLN-005 | repost-cleanup/index.ts | 本地清理列表筛选/搜索/排序，不自动重评估 |
| 658 | input repost-search-input | CLN-005 | repost-cleanup/index.ts | 本地清理列表筛选/搜索/排序，不自动重评估 |
| 659 | select repost-sort-select | CLN-005 | repost-cleanup/index.ts | 本地清理列表筛选/搜索/排序，不自动重评估 |

### 动态模板控件：45处定义

这是源码模板定义数，不是网页实时按钮数量（每个话题/活动/账号可渲染多枚）。已追踪所属模块的render→bind/事件委托→API或startJob；页面不存在的历史绑定单独标记，不能混入在用按钮。

| 模板位置 | 标识/类别 | 功能 | 触发与边界 |
|---|---|---|---|
| web/frontend/src/account/index.ts:254 | data-onboarding-action | UI-013 | 引导CTA、@角标本地ack |
| web/frontend/src/account/index.ts:320 | id=account-at-ack-btn | UI-013 | 引导CTA、@角标本地ack |
| web/frontend/src/activities/index.ts:47 | data-action, data-dynamic-id | ACT-001 | 统一参与/忽略/删除失败/加载重试/分页；参与才可能mutation |
| web/frontend/src/activities/index.ts:65 | data-action, data-dynamic-id | UI-001/002/003 | 统一参与/忽略/删除失败/加载重试/分页；参与才可能mutation |
| web/frontend/src/activities/index.ts:69 | data-action, data-dynamic-id | UI-001/002/003 | 统一参与/忽略/删除失败/加载重试/分页；参与才可能mutation |
| web/frontend/src/activities/index.ts:220 | form提交/按钮模板 | UI-001/002/003 | 统一参与/忽略/删除失败/加载重试/分页；参与才可能mutation |
| web/frontend/src/activities/index.ts:262 | id=page-prev | UI-001/002/003 | 统一参与/忽略/删除失败/加载重试/分页；参与才可能mutation |
| web/frontend/src/activities/index.ts:263 | id=page-next | UI-001/002/003 | 统一参与/忽略/删除失败/加载重试/分页；参与才可能mutation |
| web/frontend/src/jobs/index.ts:371 | data-failure-action | UI-017 | 显式失败引导，后端仍重新核验 |
| web/frontend/src/notifications/index.ts:159 | data-ntf-field | NTF-001/002/003/006 | 渠道配置/测试/开关/记录隐藏/Winner深检或本地候选删除；中奖跳转占位disabled |
| web/frontend/src/notifications/index.ts:171 | data-ntf-enabled | NTF-001/002/003/006 | 渠道配置/测试/开关/记录隐藏/Winner深检或本地候选删除；中奖跳转占位disabled |
| web/frontend/src/notifications/index.ts:176 | form提交/按钮模板 | NTF-001/002/003/006 | 渠道配置/测试/开关/记录隐藏/Winner深检或本地候选删除；中奖跳转占位disabled |
| web/frontend/src/notifications/index.ts:177 | data-ntf-test | NTF-001/002/003/006 | 渠道配置/测试/开关/记录隐藏/Winner深检或本地候选删除；中奖跳转占位disabled |
| web/frontend/src/notifications/index.ts:209 | data-ntf-event | NTF-001/002/003/006 | 渠道配置/测试/开关/记录隐藏/Winner深检或本地候选删除；中奖跳转占位disabled |
| web/frontend/src/notifications/index.ts:233 | data-ntf-record-delete | NTF-001/002/003/006 | 渠道配置/测试/开关/记录隐藏/Winner深检或本地候选删除；中奖跳转占位disabled |
| web/frontend/src/notifications/index.ts:416 | id=winner-scan-run | SC-014/NTF-004 | 渠道配置/测试/开关/记录隐藏/Winner深检或本地候选删除；中奖跳转占位disabled |
| web/frontend/src/notifications/index.ts:446 | form提交/按钮模板 | SC-014/NTF-004 | 渠道配置/测试/开关/记录隐藏/Winner深检或本地候选删除；中奖跳转占位disabled |
| web/frontend/src/notifications/index.ts:447 | data-winner-delete | SC-014/NTF-004 | 渠道配置/测试/开关/记录隐藏/Winner深检或本地候选删除；中奖跳转占位disabled |
| web/frontend/src/profiles/index.ts:142 | data-profile-activate | SYS-003/005/006/007 | 切换重启、打开运行实例、新窗口、删除Profile、新账号QR关闭；运行Profile不能删 |
| web/frontend/src/profiles/index.ts:144 | data-profile-open | SYS-003/005/006/007 | 切换重启、打开运行实例、新窗口、删除Profile、新账号QR关闭；运行Profile不能删 |
| web/frontend/src/profiles/index.ts:145 | data-profile-launch | SYS-003/005/006/007 | 切换重启、打开运行实例、新窗口、删除Profile、新账号QR关闭；运行Profile不能删 |
| web/frontend/src/profiles/index.ts:146 | data-profile-delete | SYS-003/005/006/007 | 切换重启、打开运行实例、新窗口、删除Profile、新账号QR关闭；运行Profile不能删 |
| web/frontend/src/profiles/index.ts:385 | id=onboarding-close | SYS-003/005/006/007 | 切换重启、打开运行实例、新窗口、删除Profile、新账号QR关闭；运行Profile不能删 |
| web/frontend/src/repost-cleanup/index.ts:397 | data-repost-page-kind, data-repost-page | CLN-005/006/004 | 分页/选本人repost/分别复制original或repost/暂缓恢复/显式重评估；复制original不用于删除 |
| web/frontend/src/repost-cleanup/index.ts:398 | data-repost-page-kind, data-repost-page | CLN-005/006/004 | 分页/选本人repost/分别复制original或repost/暂缓恢复/显式重评估；复制original不用于删除 |
| web/frontend/src/repost-cleanup/index.ts:506 | data-repost-select | CLN-005/006/004 | 分页/选本人repost/分别复制original或repost/暂缓恢复/显式重评估；复制original不用于删除 |
| web/frontend/src/repost-cleanup/index.ts:526 | data-repost-copy | CLN-005/006/004 | 分页/选本人repost/分别复制original或repost/暂缓恢复/显式重评估；复制original不用于删除 |
| web/frontend/src/repost-cleanup/index.ts:529 | data-repost-copy | CLN-005/006/004 | 分页/选本人repost/分别复制original或repost/暂缓恢复/显式重评估；复制original不用于删除 |
| web/frontend/src/repost-cleanup/index.ts:537 | data-repost-restore | CLN-005/006/004 | 分页/选本人repost/分别复制original或repost/暂缓恢复/显式重评估；复制original不用于删除 |
| web/frontend/src/repost-cleanup/index.ts:540 | data-repost-defer | CLN-005/006/004 | 分页/选本人repost/分别复制original或repost/暂缓恢复/显式重评估；复制original不用于删除 |
| web/frontend/src/repost-cleanup/index.ts:543 | data-repost-reeval | CLN-005/006/004 | 分页/选本人repost/分别复制original或repost/暂缓恢复/显式重评估；复制original不用于删除 |
| web/frontend/src/repost-cleanup/index.ts:708 | data-repost-copy | CLN-005/006/004 | 分页/选本人repost/分别复制original或repost/暂缓恢复/显式重评估；复制original不用于删除 |
| web/frontend/src/shell/toast.ts:36 | data-toast-action | UI-012/017 | toast动作与关闭；仅用户点击才执行对应引导 |
| web/frontend/src/shell/toast.ts:48 | form提交/按钮模板 | UI-012/017 | toast动作与关闭；仅用户点击才执行对应引导 |
| web/frontend/src/sources/ds9-panel.ts:60 | data-ds9-toggle, data-ds9-enabled | UI-007/008/SC-008 | 话题启停/删除/更新/贡献导出/添加；非抽奖审计入口隐藏 |
| web/frontend/src/sources/ds9-panel.ts:75 | data-ds9-delete | UI-007/008/SC-008 | 话题启停/删除/更新/贡献导出/添加；非抽奖审计入口隐藏 |
| web/frontend/src/sources/ds9-panel.ts:194 | data-ds9-refresh | UI-007/008/SC-008 | 话题启停/删除/更新/贡献导出/添加；非抽奖审计入口隐藏 |
| web/frontend/src/sources/ds9-panel.ts:201 | data-ds9-contribution-export | UI-007/008/SC-008 | 话题启停/删除/更新/贡献导出/添加；非抽奖审计入口隐藏 |
| web/frontend/src/sources/ds9-panel.ts:204 | data-ds9-name | UI-007/008/SC-008 | 话题启停/删除/更新/贡献导出/添加；非抽奖审计入口隐藏 |
| web/frontend/src/sources/ds9-panel.ts:205 | data-ds9-id | UI-007/008/SC-008 | 话题启停/删除/更新/贡献导出/添加；非抽奖审计入口隐藏 |
| web/frontend/src/sources/ds9-panel.ts:206 | form提交/按钮模板 | UI-007/008/SC-008 | 话题启停/删除/更新/贡献导出/添加；非抽奖审计入口隐藏 |
| web/frontend/src/watch/index.ts:105 | data-watch-remove | UI-005/006/SC-001～008 | 监控删除确认/取消；来源单源更新 |
| web/frontend/src/watch/index.ts:107 | data-watch-confirm | UI-005/006/SC-001～008 | 监控删除确认/取消；来源单源更新 |
| web/frontend/src/watch/index.ts:108 | data-watch-cancel | UI-005/006/SC-001～008 | 监控删除确认/取消；来源单源更新 |
| web/frontend/src/watch/index.ts:288 | data-action, data-source-id | UI-005/006/SC-001～008 | 监控删除确认/取消；来源单源更新 |

其它事件入口也已读取：导航快捷键/QR Escape及焦点陷阱、SSE EventSource与重连poll、主题媒体查询、Profile切换目标身份轮询、清理表格选择与确认、任务终态后后台轻量刷新。它们不新增独立业务功能计数。当前没有可确认DOM生产入口的check_updates绑定归UI-016待核实；不是凭API存在推定UI可用。

### CLI/开发脚本：34文件与其它平台入口

以下都是显式执行入口，不是Scheduler后台自动调用。‘audit/handtest/dry-run’名字不代表0写入或0网络。未来验证必须临时根+mock，不能照表直接对真实数据执行。

| 脚本 | 主入口 | 功能 | 已读执行边界 |
|---|---|---|---|
| scripts/audit_charging_activities.py | main:18 | OPS-009 | 名称含审查但会写库，不是只读命令。 |
| scripts/backfill_missing_details.py | main:26 | OPS-006 | 失败文件使用仓库旧路径；异常catch后继续，不等价当前主Job全局风控。 |
| scripts/backfill_missing_lottery_times.py | main:17 | OPS-010 | 推断日期不是官方开奖证明，不能据此自动删除转发。 |
| scripts/bili_login.py | main:15 | SYS-004 | logout修改Cookie，用户真实数据禁止在审计执行。 |
| scripts/check_ds1.py | 模块顶层 | OPS-001 | CLI检查成功不等于抽奖已入库；--force会重复读取远端。 |
| scripts/check_ds2.py | 模块顶层 | OPS-001 | CLI检查成功不等于抽奖已入库；--force会重复读取远端。 |
| scripts/check_ds3.py | 模块顶层 | OPS-001 | CLI检查成功不等于抽奖已入库；--force会重复读取远端。 |
| scripts/check_ds4.py | 模块顶层 | OPS-001 | CLI检查成功不等于抽奖已入库；--force会重复读取远端。 |
| scripts/check_ds5.py | 模块顶层 | OPS-001 | CLI检查成功不等于抽奖已入库；--force会重复读取远端。 |
| scripts/check_ds6.py | 模块顶层 | OPS-001 | CLI检查成功不等于抽奖已入库；--force会重复读取远端。 |
| scripts/check_ds7.py | 模块顶层 | OPS-001 | CLI检查成功不等于抽奖已入库；--force会重复读取远端。 |
| scripts/check_messages.py | main:119 | OPS-003 | B站只读网络仍消耗请求；不标记已读。 |
| scripts/discover_watch_users.py | main:171 | OPS-011 | 可请求很多个人空间；是手动工具，不属于参与实时查重；风险异常可能被占比函数吞成0。 |
| scripts/export_activities_seed.py | main:24 | OPS-013 | 需核对输出路径，不等价跨账号合并或完整备份。 |
| scripts/export_migrate_bundle.py | main:212 | OPS-015 | 敏感包禁止上传公开仓库；多Profile应安全退出后完整备份数据根，不能用旧工具假称完整。 |
| scripts/export_private_complete.py | main:171 | OPS-015 | 敏感包禁止上传公开仓库；多Profile应安全退出后完整备份数据根，不能用旧工具假称完整。 |
| scripts/export_private_ready.py | main:240 | OPS-015 | 敏感包禁止上传公开仓库；多Profile应安全退出后完整备份数据根，不能用旧工具假称完整。 |
| scripts/export_state_seed.py | main:23 | OPS-013 | 需核对输出路径，不等价跨账号合并或完整备份。 |
| scripts/fetch_activity_info.py | main:16 | OPS-004 | 与公共Pipeline详情阶段重叠，不是新发现来源。 |
| scripts/handtest_data_layer.py | main:15 | OPS-016 | 有写入；必须临时根；不是只读生产健康检查。 |
| scripts/import_json_to_db.py | main:42 | OPS-014 | --force可覆盖业务数据/重导动作日志，不允许随意对生产库运行。 |
| scripts/maintain_local_activities.py | main:53 | OPS-005 | --dry-run不是0网络；worker广义异常会记失败后继续，正式风险边界需单独验证。 |
| scripts/migrate_frontend_app.py | main:449 | OPS-017 | 旧app.js已移除；现在不是常规构建步骤，重新执行可能破坏稳定前端。 |
| scripts/migrate_lottery_times.py | main:28 | OPS-010 | 推断日期不是官方开奖证明，不能据此自动删除转发。 |
| scripts/participate.py | main:39 | OPS-002 | 会产生真实参与POST；默认没有Web人工UNKNOWN授权。 |
| scripts/purge_all_dead_links.py | main:19 | OPS-008 | 网络失败不能等同404；真实本地删除需备份。 |
| scripts/rebuild_all_activity_details.py | main:42 | OPS-007 | 广义异常继续；不应作为在线运行实例的无风险修复工具。 |
| scripts/render_brand_png.py | main:14 | OPS-018 | 本轮不生成资源、不擅自替换正式图标；各工具有写文件副作用。 |
| scripts/replenish_watch_users.py | main:116 | OPS-011 | 可请求很多个人空间；是手动工具，不属于参与实时查重；风险异常可能被占比函数吞成0。 |
| scripts/retry_failed_rebuild.py | main:31 | OPS-007 | 广义异常继续；不应作为在线运行实例的无风险修复工具。 |
| scripts/rollback_ds_containers.py | main:35 | OPS-012 | 会主动联网与回退进度，非正常失败恢复；不得用在真实用户数据审计。 |
| scripts/run_dashboard.py | main:14 | SYS-009 | 直接--serve不是完整托盘supervisor体验；不同版本launcher共用数据根不要混跑。 |
| scripts/run_e2e_server.py | main:27 | SYS-022 | Playwright旧selector与reuseExistingServer需先校正/核对，不应直接接真实实例。 |
| scripts/sync_brand_icons.py | main:28 | OPS-018 | 本轮不生成资源、不擅自替换正式图标；各工具有写文件副作用。 |

根入口`binggo_launcher.py`用于源码/Windows frozen；`scripts/run_dashboard.py`命令不变，默认supervisor，`--serve`是服务子进程入口而非日常独立托盘模式。正式Windows `packaging/windows/build.ps1`与spec/installer、macOS `packaging/macos/build.py`/spec、图标生成器、MCP包main与CI/站点脚本均见SYS-023～025/OPS-018～024及508行覆盖表；它们也已阅读全文，不是因文件较少被排除。

## 横向重复、遗留与安全疑点

证据等级：‘已确认’只指源码行为/矛盾已核实，除非注明，不是已对真实账号复现Bug；‘有风险但未复现’需要离线或安装环境证明；‘待进一步调查’不能猜结论。P1表示优先验证的数据/请求安全风险，P2表示能力、体验或维护边界；本轮没有证明需立刻执行的真实P0事故。

| 疑点/优先级 | 证据等级与源码依据 | 触发条件/实际行为 | 影响 | 最小后续验证方向 |
|---|---|---|---|---|
| D-001 P1 | 已确认（代码行为；真实影响未执行）；packaging/windows/installer.iss::KillBinggoProcesses:55、InitializeSetup/PrepareToInstall | 安装时taskkill /F /IM Binggo.exe /T按映像名结束所有实例，不走Profile安全取消等待 | 未先安全退出旧版就运行Setup时，多个账号任务/进程可能被强制结束；并非V112a正常托盘退出 | 安装前用户安全退出全部实例；未来独立修复安装流程，不在审计中测试真实杀进程 |
| D-002 P1 | 有风险但未复现；src/login_onboarding.py::_init_profile_db:171；binggo_launcher.py::main:605（参数定义610～615） | 新Profile DB初始化执行[sys.executable,-c,code]；frozen时该exe是Binggo而非Python | 源码创建成功不能证明安装版同路可用；launcher参数没有-c解释执行契约，可能创建失败 | 先用临时frozen环境/mock sys.executable复现新账号建立；不可直接改用户Profile或宣称真实Bug已复现 |
| D-003 P2 | 已确认（保留方向；用户丢记录未复现）；src/winner_scan/service.py::_save_candidates:460 | discovered_at倒序后ordered[-500:]保留较旧500条；pending preparation额外保留 | 超过500时最新已完成通知候选可能先消失；不能因此断言所有pending通知漏发 | 临时501/600候选精确测保留顺序与pending恢复；先确认产品保留意图 |
| D-004 P1 | 已确认局部吞异常；全局影响待复现；src/winner_scan/service.py::WinnerScanService._lookup_nickname:741 | 正式风险只blocked后续昵称补读并return空串，保留UID回退候选 | 停止了昵称请求，但risk可能不被Job/Scheduler全局暂停感知，后续非昵称阶段是否请求须验证 | 离线注入-352/-509/429并记录完整后续调用；不要为补昵称继续B站请求 |
| D-005 P2 | 已确认构建检查不一致；错误产物未复现；packaging/windows/build.ps1:1；Invoke-BinggoInnoSetup | 多数native工具缺逐个LASTEXITCODE检查；ISCC有检查，但缺ISCC可跳过且旧SetupPath存在会打印 | 构建日志‘完成/安装包存在’不足证明本次Setup由本次HEAD生成 | 独立模拟native失败/旧产物时间戳；真实打包前核验fresh时间/hash，不以文件存在代替成功 |
| D-006 P2 | 已确认接口能力差异；mcp/binggo_mcp/client.py::BinggoClient:43；server.py::job_participate:436 | 默认固定8787；MCP参与没有Web的manual_unknown_retry参数；instructions旧冲突说法 | 双Profile可能连非预期端口；MCP/CLI不是当前人工UNKNOWN恢复的完整替代 | 先比对runtime/profile/port与Job ID；未来独立补明确选择/文档，不让工具自动授权UNKNOWN |
| D-007 P1 | 已确认遗留路径/异常分支；后续请求风险未复现；scripts/backfill_missing_details.py::main:26；scripts/rebuild_all_activity_details.py::main:42；scripts/replenish_watch_users.py:1 | 旧ROOT/data/output或config路径及catch Exception后继续；占比失败可变0 | 多个Profile旧CLI输出可串路径；formal可能不按主Job暂停；只是显式工具，不会自动运行 | 不要对在线真实数据根执行；临时根注入风险后数请求；分工具修正，不重构主Pipeline |
| D-008 P2 | 已确认能力边界，非新Bug定论；src/sources/recent_articles.py::check_recent_articles:61；ds1_xiaozhuli.py::check_update:24；ds9_topics.py::check_update:609 | DS1/3最新1容器，DS2/4～7最多5篇，DS9最多3页，Following history15天 | 长时间未刷新/大量发布会超窗口；incomplete不等于已经全历史覆盖 | 用超过上限fixture确认提示/保守checkpoint；不得通过推进checkpoint隐藏缺口 |
| D-009 P2 | 已确认持久化范围；src/following_feed.py::_run_pipeline_batches:555；src/pipeline/refresh_all_pipeline.py::run_new_links_pipeline:388 | 只有Following当前25条批次commit；公共单次Pipeline末尾保存；非抽奖不永久入活动库 | 其它源中途异常可重做内存分类；Following非抽奖/未完批次仍可重复读取或分类 | 先SC-011离线中断对照；非抽奖缓存需独立考虑版本失效，不能本轮视作永久false |
| D-010 P2 | 已确认返回/游标语义不同；src/following_feed.py::scan_following_feed:622；tests/test_following_feed_checkpoint_v16.py | 部分blocking/partial返回success但保留旧checkpoint | 用户可能将‘任务完成’理解为窗口已扫全；旧活动缺口不能只查Job success | 读取stop_reason/catch_up_complete/blocking；未来最小提示，不放松游标 |
| D-011 P2 | 已确认设计限制；web/auto_scheduler.py::_scan_offset_minutes:380 / _runtime_scan_slots:407；src/bilibili_rate_limit.py:122 | rank×15分钟是计划偏移，不是跨进程互斥；RPS每进程 | 扫描耗时>15分钟、原始base不同仍可能重叠；没有中央限流/锁 | 临时时钟长Job与不同base验证；用户知晓残余并发，勿擅加跨进程框架 |
| D-012 P2 | 待进一步调查常规生产调用者；src/manual_participation_retry.py::retry_unknown_action:99；web/actions.py::_run_action_impl:1253；web/frontend/src/runtime/index.ts::bindCheckUpdates:51 | ACT-014单动作函数、CLN-010维护cycle、UI-016旧更新按钮有代码/兼容或测试，常规生产入口不确认 | 不能把保留实现算当前用户必需流程；删除它们也需先确认兼容调用 | 仓库直接调用/白名单/HTML已查；外部私有调用不在源码证据内；不擅删 |
| D-013 P2 | 已确认源码/测试契约矛盾；未运行E2E；web/frontend/e2e/smoke.spec.ts:59；web/frontend/src/sources/refresh-all-entrypoints.test.ts:1；web/frontend/index.html:470 | Playwright仍点击data-action=refresh_all；当前页面移除该按钮且Vitest断言不应存在 | 完整pytest/Vitest历史通过不证明Playwright smoke可通过 | 未来只修已有E2E选择器/场景契约后离线执行；不删安全断言 |
| D-014 P2 | 已确认现行行为与历史要求不同；是否改由用户决定；web/frontend/src/shell/nav.ts::bindNavigation:75；web/frontend/src/jobs/index.ts::setLogDockOpen:590；styles/styles.css | 实际初始圆形日志入口；蓝色展开卡有▼收起；终态不强制折叠用户手动打开面板 | 历史‘标题居中▲/完成必收起’不是当前已实现事实；日志内容保留 | 先UI-009/010人工只读查看，记录期望差异，独立UX版本，不把旧需求当源码 |
| D-015 P2 | 已确认保护并不统一；src/lottery_actions.py::like_dynamic:671 / follow_user:694；src/participation.py::_reserve_click:774；participation_log.py | 点赞/关注有限默认重试；预约零重试但无独立长期guard；升级前已裁剪unknown无法还原 | 不能宣传全mutation永久屏障或所有POST零重试；预约日志裁剪后的保护需独立研究 | 保留当前成熟comment/favorite/repost契约；新增屏障先调查目标身份与兼容，不清guard |
| D-016 P2 | 待进一步调查真实平台/安装验收；src/windows_tray.py::WindowsTray；packaging/windows/binggo.spec；src/lottery_api.py | 静态看资源与线程不能证明Explorer真正可见或当前API字段还有效；本轮未运行安装包 | 不能说V117人工验收/真实扫码/平台状态可靠性已通过 | 用户在全部旧实例安全退出后独立安装烟测；只读/离线结论与真实证据分开 |
| D-017 P2 | 已确认CLI与Web契约不同；src/check_cli.py::run_check:12；web/actions.py::_run_source_refresh:704 | check_ds CLI只检查/保存候选与source checkpoint，不执行活动分类入库；Web在Pipeline后提交 | 拿CLI检查成功当活动入库成功会误判；混用路径可能影响后续是否检查容器 | 离线对照CLI/Web；先确认调用目的，不把所有来源入口强行视作同Pipeline |
| D-018 P1 | 已确认当前允许人工例外；产品接受度待核实；src/repost_cleanup.py::delete_reposts:2506；manual_review_confirmed分支:2634 | 当前选中删除允许额外manual_review_confirmed人工声明；不是最初V1只official safe候选 | 普通LLM/manual_review不可靠开奖，但可经明确用户例外进入人工删除；连续safe清理仍严格 | 人工验证前明确风险接受；mock失败/UID/orig核验；禁止实际批量DELETE |
| D-019 P2 | 已确认静态验证和本机信任边界；web/frontend/src/main.ts:1及多模块@ts-nocheck；web/security.py:1 | Typecheck不覆盖所有迁移模块；loopback+Origin不是完整认证/同Profile身份证明 | typecheck PASS不能证明全部TS逻辑正确；本地进程/多端口仍须目标核验 | 前端fixture/in-flight/结构字段测试；不公网暴露；不拿类型Gate代替运行验证 |
| D-020 P2 | 已确认只有枚举/配置，生产者待确认；src/notification/service.py::EVENT_IDS:51；web/frontend/src/notifications/index.ts::EVENT_PRODUCER_PENDING:47 | winning_confirmed/platform_risk/auto_task_error/auto_task_completed未找到生产dispatch | 配置可见或模型支持不等于真实中奖确认/风险/任务完成推送 | 先核对NTF-006；测试渠道不代表事件已接入，勿伪造中奖事实 |
| D-021 P1 | 已确认异常/claim分支；跨恢复重复风险未复现；src/comment_probe.py::run_comment_recovery_probe:30 / _unknown_exception:26；src/lottery_actions.py::comment_dynamic_probe；src/comment_breaker.py::clear_comment_breaker:220 | probe用breaker in_flight/排除集合而非普通comment pending；POST抛RuntimeError含formal可被归unknown；clear会清排除集合 | 不能套用普通评论永久guard及formal全局传播保证；旧unknown探测在另一目标恢复成功后是否再次进入普通参与须专项证明 | 离线做probe A超时→probe B成功clear→A普通参与/重启，另注入-352/-509/429检查后续请求与全局pause；本轮不改代码 |

### 为什么相似模块暂时不能合并

- DS、Following、WATCH能发现同一个抽奖，但来源边界和恢复状态不同；共用规范化/活动入库去重，不代表可共用一个游标。
- Winner是消息线索与通知账本，不是抽奖活动Pipeline；中奖词不是官方中奖确认。账号角标读、Winner独立cursor与通知claim也不是一个状态。
- participations是整体参加事实；repost_guard/comment/favorite是动作结果屏障；repost_history是真实历史身份账本；cleanup assessment是删除资格。必须保留事实与资格分离。
- retries差异按动作/请求风险设计：读取可有限重试；comment/favorite/repost/delete零自动重发；点赞/关注现有明确意图有限重试。不能为统一代码随意扩大mutation重试。
- 手动UNKNOWN完整恢复只在用户当前点击授权及可靠身份下发生；自动参与、CLI/MCP、DELETE不自动继承该授权。
- 旧auto cleanup命名、legacy JSON路径常量、后台但非Scheduler的通知恢复、历史TS迁移脚本都需要区分真实入口，不能据名称发明定时任务或JSON保存位置。

## 尚未核实与验收交接

静态实现已覆盖508/508文件、27补充配置/资源、90方法路由、16通用Job、34MCP工具、76静态控件及45动态模板；遗留/未接入常规生产链按ACT-014、CLN-010、UI-016、NTF-006四组标记。所有137项均可按稳定编号填写计划反馈。

本轮没有pytest/Vitest/build、真实B站/LLM/通知调用、本地业务HTTP、Profile DB读取或运行进程操作。Windows Explorer图标显示、真实安装包与HEAD/依赖完整性、当前平台API响应字段、真实账号旧数据的具体资格、外部私有调用者及用户安装版人工验收都待核实。仓库测试存在与断言读取，不等于测试本轮通过。

建议从UI-001（现有页面只读列表/搜索/筛选/翻页，核对账号身份）开始；随后用临时Profile/mock先验SC-011中断落盘、SC-010/013恢复、ACT-013 UNKNOWN、SYS-011/013生命周期。不要以真实参与或DELETE作为第一项；扫描真实B站虽无mutation也需用户独立授权。

后续反馈格式：功能编号、版本/HEAD、Profile/端口（不含凭据）、操作步骤、实际/期望结果、脱敏日志、计划状态。先定位单个事实，独立最小修复版本；不在本报告回合顺便改代码/清锁/重置guard。
