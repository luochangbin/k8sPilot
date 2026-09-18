# Diagnosis Center（诊断中心）

依据：`docs/diagnosis-center-handoff.md`（MVP 规格）。范围：Headlamp 侧边栏「智能诊断」入口 + 诊断中心列表 + 详情（结论/证据/建议/时间线/告警）+ 未读徽标；后端新增会话查询、未读、未解析告警与 Timeline 接口。不改动人工诊断入口与既有 API 契约。

## 实现文件

| 领域 | 文件 |
|---|---|
| Store | `agent-service/app/store.py`：`diagnosis_read_receipts` 表 + 索引、`list_sessions`（keyset+filter+unread 投影）、`list_notifications`/`count_unread_notifications`、`list_unresolved_alerts`、`mark_diagnosis_read`、`get_alert_projection` |
| 校验/游标 | `agent-service/app/center.py`：opaque keyset 游标（**绑定过滤集**）、枚举/ISO/UUID 校验 |
| Timeline | `agent-service/app/timeline.py`：JSONL 白名单投影 + byte-offset 分页 |
| API | `agent-service/app/main.py`：Center 四个端点 + `/timeline`；旧 `GET /api/v1/diagnoses`（数组）不变，旧 detail 只增 nullable `alert` |
| 插件 | `headlamp-plugin/ai-diagnosis-plugin/src/`：`types.ts`、`api.ts`、`viewer.ts`、`useDiagnosisNotifications.ts`、`DiagnosisCenter.tsx`、`DiagnosisDetail.tsx`、`index.tsx`（路由/侧边栏/徽标） |
| 测试 | `agent-service/tests/test_center.py`、`src/viewer.test.ts`、`src/api.test.ts` |

## API

```text
GET  /api/v1/diagnosis-center/sessions?limit&after&status&trigger&resource_kind&namespace&name&uid&since&until&viewer_id
GET  /api/v1/diagnosis-center/notifications?viewer_id&limit&after
POST /api/v1/diagnosis-center/sessions/{id}/read          {"viewer_id": "<uuid>"}
GET  /api/v1/diagnosis-center/unresolved-alerts?limit&after
GET  /api/v1/diagnoses/{id}/timeline?after&limit
GET  /api/v1/diagnoses/{id}                               (旧接口，仅增 nullable alert)
```

- `limit` 1..100（timeline 1..200）；非法枚举/时间/游标/UUID → **422**；未知诊断 → **404**；未终态标记已读 → **409**；已读幂等 → **200**。
- 会话游标为 keyset `(created_at, diagnosis_id) DESC`，**绑定过滤集**（改过滤条件复用旧游标 → 422）。
- `unread`：无 `viewer_id` 时为 `null`；有则按该 viewer 的 receipt 计算。
- 通知 `unread_count` 为**完整过滤集计数**（非当前页）：`trigger=alert`、终态 `completed|failed`、`eval_run_id IS NULL`、无该 viewer receipt；`failed` 与 `completed+insufficient_evidence` 均计入。
- `unresolved-alerts` 只返回 `state=unresolved_target AND diagnosis_id IS NULL` 的生命周期（`id/alertname/starts_at/latest_alert_at/state/target`），不产生未读、不构成第二套诊断。

### Timeline 投影白名单

仅返回 `id/seq/timestamp/kind/title/status/duration_ms/failure_layer`；`tool_call→tool_completed`、root started→`running`、root finished→`diagnosis_completed|diagnosis_failed`、`llm_final` 为 `result_drafted`（非终态）。**不返回** args_summary、raw error、Prompt、日志或完整输出。尾部半行不消费；坏行/超长行跳过并 `gap=true`；单页 ≤1000 完整行、单行 ≤64KiB；文件缺失/未启用 → `pending`/`unavailable`，不影响诊断结果。

## UI

- 路由：`/ai-diagnosis`（Center）、`/ai-diagnosis/:diagnosisId`（Detail）；侧边栏条目 `智能诊断`（`mdi:stethoscope`）；AppBar 徽标使用**单个 aria-live 语义状态**（如「3 个新的自动诊断」），仅在计数变化时更新。
- 轮询：Center 5s、Detail 运行态 2s、通知 15s；隐藏标签页暂停、`AbortController`/ generation 防旧响应覆盖、错误指数退避上限 30s 且不清空旧数据；过滤与分页状态写入 URL。
- 资源详情页保留手动入口，并新增「查看完整诊断」链接（按资源 `uid` 过滤 Center）。
- 未读只在「页面可见且终态详情渲染成功」时自动标记；失败保留未读并提示重试（不误报为诊断失败）。

## 审阅修正（2026-09-17，第 1 轮）

针对外部审阅的 5 个问题（3×P1、2×P2）已核实并修复（均属实）：

1. **列表/详情丢失集群前缀（P1）**：新增 `src/routes.ts`，所有跨页链接统一走 `Router.createRouteURL`（`centerUrl()`/`detailUrl()`）；Center 列表项、Detail 返回链接与资源详情页「查看完整诊断」全部改用，并新增 `routes.test.ts` 断言集群前缀。
2. **时间线不随诊断更新（P1）**：Detail 在诊断运行期随状态轮询**增量拉取时间线**（按 `next_after` 追加），终态时再补一次最终刷新；`pending` 重试改为**基于定时器的独立循环**（不再依赖状态变化触发），超过 30s 置 `unavailable`。新增页面测试断言时间线持续追加。
3. **部署无法满足持久化（P1）**：新增 `agent-service/deploy/pvc.yaml`（`agent-service-data`，2Gi，默认 StorageClass），Deployment 的 `data` 卷由 `emptyDir` 改为该 PVC，并显式设置 `TRACE_DIR=/data/trace`（与 `DIAGNOSIS_DB=/data/diagnoses.db` 同卷）。四个清单均通过 `kubectl apply --dry-run=client` 校验。
4. **切换详情时旧响应污染（P2）**：Detail 引入 `runRef` 代次守卫——`diagnosisId` 变化即递增代次，晚返回的时间线/状态响应与已读回调在代次不匹配时直接丢弃（含清空 pending 定时器）。新增测试：切换到 B 后，A 的迟到时间线不会渲染。
5. **损坏 Trace 导致 500（P2）**：`timeline.project_event` 对 `attributes` 非对象抛出 `MalformedEventError`，reader 捕获后跳过该行并置 `gap=true`（不再 500）。新增回归：`{"kind":"tool_call","attributes":"oops"}` → 200 + `gap=true` + 仅返回完好事件。

验证：`agent-service`+`eval` **117 passed**；插件 `tsc` 0 / `eslint` 0 / `vitest` **11 passed**（4 个测试文件，含 Detail 生命周期与路由）；`npm run build` 成功并安装。

## 审阅修正（2026-09-17，第 2 轮）

针对时间线终态截断/重复问题（P1，属实）修复：

- **串行化 + 去重**：`refreshTimeline` 用 `timelineInFlightRef` 串行化请求——并发触发合并为一次补跑（`timelineRerunRef`）；追加前按 `item.id` 去重（`seenIdsRef`），消除“初始加载 + 完成刷新并发读同一游标”导致的重复事件。
- **按 `has_more` 续读**：单次刷新内循环消费（上限 `MAX_PAGES_PER_DRAIN=20` 防病态），不再只读第一页。
- **终态继续消费并等待完成事件**：诊断进入终态后启动 `TERMINAL_WAIT_MS`（30s）窗口，只要尚未看到 `diagnosis_completed`/`diagnosis_failed` 就继续拉取；终态事件出现即停止，窗口到期仍未出现则停止（已有事件保留）。轮询条件相应改为「运行中 → 持续刷新；终态 → 窗口内等待终态事件」。

测试补充（Detail 共 5 项）：`has_more` 单次刷新内续读两页、同一 `id` 不重复追加、终态后继续轮询直到终态事件到达；另有既有「持续追加事件」「切换诊断丢弃迟到时间线」两项。`vitest` **14 passed**；`tsc`/`eslint` 0。

## 审阅修正（2026-09-17，第 3 轮）

再核实 2 个 P2 边界问题（均属实）并修复：

1. **半行写入触发重复请求**：`has_more=true` 但 `next_after` 未前进（尾部半行未消费）时，原实现会在单次刷新内连续请求同一位置至多 20 次。现当 `page.next_after <= before` 即**结束本轮读取**，等待下次轮询（游标必然推进时才继续翻页）。
2. **等待完成事件超时缺少提示**：终态下 30s 窗口到期仍未收到 `diagnosis_completed`/`diagnosis_failed` 时，保留已有步骤并显示提示「时间线可能不完整：等待完成事件超时，已保留现有步骤。」，提供**刷新**按钮——点击后重新开启等待窗口并立即拉取；终态事件到达后提示自动消失。

测试补充：**游标不前进时单次刷新不发过多请求**（≤2 次/轮询）、**超时提示 + 刷新后取到晚到事件且提示消失**。`vitest` **16 passed**（Detail 7 项）；`tsc`/`eslint` 0。

## 审阅修正（2026-09-18，第 4 轮）

1. **未读按钮无法进入列表**：核对 SDK 后确认写法本身是官方惯用（官方插件同样 `useHistory` + `Router.createRouteURL`），因此未归因于缺前缀，而是**让失败可见并可定位**：
   - 徽标抽成 `DiagnosisNotificationsBadge.tsx`，改用 `Link to={centerUrl()}`（锚点导航，地址栏必变，可右键/中键；不携带旧筛选、不标记已读），并暴露 `data-testid`/`data-href` 便于检查；
   - `routes.ts` 对 `createRouteURL` 返回 `''`/`'/'` 的情况**记录 console.warn 并回退到插件路径**（此前 push `''` 等于无动作，属静默失败点）；
   - 路由组件包 `ErrorBoundary`，渲染崩溃不再是白屏而是可见错误文本。
   - **仍需人工验证**：点击徽标后地址栏应为 `/c/<cluster>/ai-diagnosis` 且显示列表；若控制台出现 `createRouteURL(...) returned ""` 警告，则说明路由注册/时序问题（而非 URL 拼接）。
2. **资源名称/Namespace 筛选**：Center 新增 `Namespace`、`资源名称` 受控输入（保留 UID），接入 URL 参数（`status/trigger/namespace/name/uid`）并**在条件变化时删除 `after`**；输入受 URL 驱动（刷新/前进后退可恢复），Enter 或失焦提交；「清空筛选」清空全部条件；请求直传后端精确匹配。
3. **根因展示以描述为主**：列表根因优先显示 `summary.root_cause`（独立成行、最多两行、超出省略，`Tooltip` 悬浮/键盘聚焦显示完整文本，自动换行、最大宽度 420），`root_cause_code` 降为次要标签；仅当无描述时用错误码兜底；`insufficient_evidence` 时明确显示「证据不足，未确认根因」（不误显示为已确认），详情页仍保留完整内容。

测试补充（插件 **25 passed**，6 文件）：路由空值回退两例、徽标链接 href/零未读仍可导航、筛选写入 URL 且不含 `after` 并透传请求、清空筛选、根因描述优先 + Tooltip + 代码标签、证据不足优先、无描述时用代码兜底。

## 审阅修正（2026-09-18，第 5 轮）

按反馈调整筛选形态：

1. **Namespace 改为下拉框**：新增后端 `GET /api/v1/diagnosis-center/namespaces`（`diagnoses` 中 distinct namespace，排序去空），Center 用 `TextField select` 渲染（含「全部」），来源为真实数据而非当前页；下拉列表每 60s 刷新一次，拉取失败不阻塞列表。
2. **资源名称改模糊匹配**：后端 `name` 过滤由等值改为 `LIKE %value%`（`ESCAPE '\'`，转义用户输入中的 `%`/`_`/`\`），Namespace 仍为精确匹配，二者可组合；不只在当前已加载页内过滤。
3. **移除 UID 筛选控件**：Center 不再提供 UID 输入；后端仍保留 `uid` 参数，资源详情页「查看完整诊断」的 `?uid=` 深链与 `uid` 优先级逻辑不变。

测试补充（后端新增 2 项）：名称子串命中、`name` + `namespace` 组合且不跨 Namespace 混入、通配符被转义（`%` 不命中）、namespaces 端点去重排序。插件新增/调整：下拉选择写入 URL 且不含 `after`、模糊名称透传、断言无 UID 控件。合计：后端 **119 passed**；插件 **26 passed**（6 文件）；`tsc`/`eslint` 0。

## 批量已读与运维脚本（2026-09-18，第 6 轮）

1. **全部标为已读**：Center 标题栏右侧（`SectionBox.headerProps.actions`）显示「未读 N 条 + [全部标为已读]」。
   - 后端 `POST /api/v1/diagnosis-center/notifications/read-all {viewer_id}`：在**同一把锁内快照**当前未读 id 再批量写入 receipt，因此**处理期间新完成的诊断保持未读**；口径与通知计数一致（`trigger=alert`、终态、`eval_run_id IS NULL`、无 receipt）；幂等（第二次返回 0）；viewer 隔离；非法 viewer 422。
   - 前端：无未读时禁用；点击后按钮显示「处理中…」，成功后刷新徽标与列表未读标记并递增跨标签页 revision，失败保留原状态并提示「标记失败，请重试」；Tooltip 说明作用范围（含当前筛选/分页之外）。
   - 实测：`before=6 → read=6 → after=0`。
2. **运维脚本 `agent-service/restart-agent.ps1`**：杀旧进程 → **有界等待端口释放** → 有界解析 connector → 启动 → `HasExited`/错误日志快速失败 → 健康检查 → 校验监听者属于**新进程树**。
   - 之前“重启看起来卡住”的两条根因已修正：(a) 启动子进程会继承调用方标准句柄，`| Out-String` 会一直等 → 现在 **stdin 重定向到空文件**，并明确不要 pipe 输出（需要日志就 `*> restart.log`）；(b) 端口未释放就启动会让新进程绑定失败而**旧进程继续应答**（表现为“重启了还是旧行为”）→ 现在先确认端口释放，再校验监听者属于新进程树（Windows 下 venv python 是重定向器，监听者是子进程，故按进程树校验）。

## 审阅修正（2026-09-18，第 7 轮）

1. **Home 页（未选集群）点徽标 404**：中心路由是**集群作用域**，未选集群时 `createRouteURL` 返回 `/`，回退路径 `/ai-diagnosis` 不带集群前缀 → 命中 404。改为用 `Utils.getCluster()` 判断：**未选集群时徽标禁用并提示「请先选择集群」**（仍显示未读数），不再触发导航；进入集群后恢复为链接。
2. **详情页「调查过程」缺少 √**：把资源页的勾选渲染抽成共享组件 `src/InvestigationSteps.tsx`（含 `data-testid="step-check"`），`DiagnosisSection` 与 `DiagnosisDetail` 共用，两处样式与语义一致。

测试补充（插件 **31 passed**，6 文件）：未选集群时徽标 `disabled` 且无 `href`、未读数仍显示；详情页每个调查步骤渲染一个勾选（2 步 → 2 个 `step-check`）。`tsc`/`eslint` 0，`npm run build` 成功并安装。

## 审阅修正（2026-09-18，第 8 轮）

1. **旧数据无法一键已读**：根因是**两处口径不一致**——Center 列表的 `unread` 对任意诊断都算（无 receipt 即未读），而「全部标为已读」只处理 `trigger=alert`+终态+非 eval，导致旧 manual/eval 记录在列表里长期显示未读且清不掉。现 `list_sessions` 的 `unread` 与通知统一为同一口径（alert 且终态且 `eval_run_id IS NULL`），manual/eval 一律返回 `false`；列表可见的未读与批量已读完全一致。实测：`total=100 alert_unread=6 manual_unread=0`，`read-all read=6 → unread_after=0`。
2. **列表改为分页**：去掉底部「加载更多」，改为 **上一页 / 第 N 页 / 下一页**。后端为 keyset 游标（不允许大 offset），故用**内存游标栈**按页前进/后退，并把 `page` 与 `after` 写入 URL；刷新或浏览器前进/后退时**有界重放**（≤当前页）重建游标栈，页面越界时回退到第 1 页；修改/清空筛选同时删除 `page` 与 `after`，从第 1 页查询；轮询仅在第 1 页进行，避免覆盖用户当前页。
   - 附带修复：加载 effect 曾依赖 `history` 对象，测试中该对象每次渲染都变会导致 **effect 自循环/挂起**；改用 `historyRef` 后不再依赖不稳定对象。

测试补充（插件 **32 passed**）：翻页写入 `after`+`page` 且请求带上游标、第 2 页禁用「下一页」/第 1 页禁用「上一页」、返回第 1 页时清除 `after`/`page`；后端测试新增「未读口径与批量已读一致（manual/eval 不为未读，read-all 清空可见未读）」。

## 分页风格对齐 Headlamp（2026-09-18，第 9 轮）

按反馈改用与 Headlamp 列表页一致的 **MUI `TablePagination`**（Headlamp 的 `Table` 组件底层就是它）：

- 选项与默认值沿用 Headlamp：`rowsPerPageOptions = [15, 25, 50]`（对应其 `defaultTableRowsPerPageOptions`），并用 `@kinvolk/headlamp-plugin/lib/helpers` 的 `getTablesRowsPerPage/setTablesRowsPerPage` 读写 `tables_rows_per_page`，与 Headlamp 其它表格**共享同一持久化设置**。
- 控件：`showFirstButton/showLastButton=false`、`labelRowsPerPage="每页行数："`、`labelDisplayedRows` 只显示范围（keyset 无法给出总数，不显示伪造总数）；`SelectProps.inputProps['aria-label']='rows per page'` 便于访问与测试。
- 与 keyset 的衔接：`onPageChange` 只接受 ±1 页（无跳页/末页）；`count` 在有下一页时为 `-1`（未知总数），到最后一页时用已见行数计算，使「下一页」自动禁用；改变每页行数会持久化设置并从第 1 页重新查询（清除 `page`/`after`）。

测试补充（插件 **33 passed**）：`TablePagination` 前进/后退按钮与禁用态、翻页写入 URL 与请求游标、每页行数改为 25 时持久化并重置到第 1 页。

### 修正：SDK 子路径不可外部化（2026-09-18，第 10 轮）

现象：页面报 `Cannot read properties of undefined (reading 'getTablesRowsPerPage')`。

根因：插件构建只把**白名单** SDK 子路径映射到宿主全局（`@kinvolk/headlamp-plugin/lib/{Router,CommonComponents,K8s,ApiProxy,Crd,...}` → `pluginLib.*`）；`@kinvolk/headlamp-plugin/lib/helpers` **不在名单内**，运行时该模块为 `undefined`，取 `default.getTablesRowsPerPage` 即抛错。

修复：本地实现 `src/tablesRowsPerPage.ts`（`getTablesRowsPerPage/setTablesRowsPerPage`），读写**同一个 localStorage 键 `tables_rows_per_page`**，仍与 Headlamp 其它表格共享「每页行数」设置；同时移除对 `lib/helpers` 的导入。构建产物已确认不再引用 `lib/helpers`。

教训（供后续插件开发）：插件只能用 SDK 明确外部化的子路径；引入其它子路径前先查 `node_modules/@kinvolk/headlamp-plugin/config/vite.config.mjs` 的 `externalModules` 白名单，否则运行时为 undefined。

## 审阅修正（2026-09-18，第 11 轮：12 条）

按外部审阅逐条处理：11 条属实已改，第 12 条为行为分析（不改代码）。

1. **告警上下文未进入调查**（属实）：`prompts.user_message` 现接收 `AlertContext`，把 alertname/status/starts_at/fingerprint/labels/annotations 与 **connector 触发快照**（有界 4000 字符，标注“仅初始线索、非工具证据，需用工具核实”）注入首条消息，并给出 starts_at 时间基准指引（query_metrics/query_logs 的窗口围绕该时刻）；manual 保持“人工触发”且不含告警块。
2. **限流早于去重**（属实）：`_handle_alert` 调整为 resolved → claim/去重 → 仅在**真正要新建诊断**时校验 limiter；429 时 `release_alert_lifecycle` 回滚空 claim。unresolved 告警不消耗诊断额度（重复投递本就被生命周期折叠）。
3. **同指纹跨实例被误去重**（属实）：claim 时比较 `starts_at`，同指纹但新 `starts_at` 视为新实例——归档旧生命周期并新建，避免“丢失 resolved 后不再诊断”；迟到的旧实例 resolved 不关闭新实例（补回归测试）。
4. **viewer 回退 id 非 UUID**（属实）：前端统一产出规范 UUIDv4（`randomUUID` → `getRandomValues` → 末位模板），并**替换已存的非规范值**；后端 `validate_viewer_id` 收紧为规范 UUIDv4（canonical 文本且 version=4）。
5. **unresolved 告警不可见**（属实）：通知 feed 并入 `state=unresolved_target AND diagnosis_id IS NULL` 的告警（`kind=unresolved_target`、`diagnosis_id=null`），计入未读数；resolve/close 后自动消失。
6. **新浏览器把历史全标未读**（属实）：新增 `viewer_state(viewer_id, first_seen_at)` 基线，未读口径加 `created_at > 基线`；新 viewer 首次打开只看到“此后新产生的”未读。
7. **新完成项难定位**（属实）：`sessions?unread=true`（需 viewer_id，否则 422）+ Center「只看未读」开关（写入 URL）；app-bar 徽标有未读时直接导航到 `?unread=true`。
8. **退避未生效**（属实）：Center/Detail 由固定 `setInterval` 改为**递归 `setTimeout`**，每次 tick 重读 delay，失败才真正放大间隔（上限 30s）。
9. **失败工具调用被显示为成功步骤**（属实）：仅**成功返回**的工具调用进入 `investigation_steps`；失败仍完整记录在 trace/执行时间线。详情页「调查过程」加说明文案。
10. **「实时证据」在调查中为空易误解**（属实）：更名「关键证据」并说明“来自最终结论，进行中以执行时间线为准”，空态区分进行中/无。
11. **创建诊断与关联生命周期非原子**（属实）：新增 `store.create_with_alert_link`（单事务 INSERT+UPDATE，UPDATE 影响行数≠1 则回滚）后再启动线程；线程启动失败将诊断标记 `failed`（不再静默 queued），并保留关联使重复投递可见地去重。`_start` 支持 `lifecycle_id`。
12. **Connector 批量部分失败会整批重试**（不改代码）：`Summary{accepted/unresolved/failed/rate_limited,results}` 已是现成可观测面，`RetryableStatus()` 仅全 429 返回 429、部分失败返回 502。第 2 条修完后，重试中**已完成**的告警在 Agent 侧是零成本 dedup。未加 Prometheus 指标：当前单实例、响应体即汇总，待确有跨进程告警风暴观测需求再引入。

13. **失败态被当作“证据不足/进行中”渲染**（联调时发现）：Agent 在校验失败（如「目标资源已重建」）时会写入一个**空 result**，详情页据此显示“证据不足，无法确定唯一根因 / 置信度 unknown”，第 10 轮的新文案还会说“调查进行中”。现在 `status=failed` 时结论区改为“诊断未完成，无结论；请查看上方错误信息与下方执行时间线”，关键证据区文案与空态按状态区分（失败→“（诊断失败，无证据）”，进行中→“调查进行中…”，完成→“来自最终诊断结论”）。

真机验证（重启 Agent 后，connector Pod `10.244.1.4:8080`）：

```text
namespaces=aiops-eval, loki-demo            # 已排除评测命名空间
fresh viewer unread=0                        # 第 6 条基线
AM 触发 e2e-review-1 -> diag_b0d166cbb178 completed
  alert={fingerprint:36daf397f4768786, starts_at:2026-09-18T02:10:59Z, state:open}
  symptom 明确引用“触发 PodCrashLooping 告警”   # 告警上下文确实进入 prompt
firing starts=A  -> deduped=False
firing starts=A  -> deduped=True  same_id
firing starts=B  -> deduped=False new_id      # 第 3 条
late resolved A  -> resolved_noop closed=False
unresolved       -> first_deduped=False, repeat_deduped=True
notifications: unresolved 后 unread=1 kinds=unresolved_target，resolve 后 unread=0
```

测试：后端 **131 passed**（新增：告警上下文进 prompt / manual 无告警块 / 失败工具不计步骤 / 去重不吃限流额度 / 新 starts_at 新生命周期 / 迟到 resolved / 线程启动失败 / 新 viewer 基线 / unresolved 通知 / unread 过滤）；插件 **37 passed**、`tsc` 0、`eslint --max-warnings 0` 0、build 成功并安装。

另修运维脚本一个真实缺陷：`restart-agent.ps1` 原先**先杀旧 Agent 再解析 Connector IP**，集群不可达时会把服务留在 down（本次 VM 掉线即触发）。现改为**先解析**并支持 `-ConnectorBaseUrl` 用已知地址启动，解析失败不再影响运行中的服务。

## 验证记录

自动化（本机）：

- `agent-service`：**92 passed**（含 `tests/test_center.py`：顺序/过滤/UID、unread(null/viewer)、422 矩阵、游标绑定过滤集、alert 投影、未读计数口径、read 幂等/409/404/422/viewer 隔离、unresolved、timeline 投影/gap/半行/分页/越界/pending/unavailable；第 11 轮新增 viewer 基线、unresolved 通知、unread 过滤、alert 上下文进 prompt、失败工具不计步骤、去重不吃限额、新 starts_at 新生命周期、线程启动失败）。
- `eval`：**39 passed**（无回归）。
- 插件：`tsc --noEmit` 0 错误、`eslint --max-warnings 0` 0 问题、`npm run build` 成功并安装到 `%APPDATA%\Headlamp\Config\plugins\ai-diagnosis-plugin\`、`vitest run` **37 passed**（viewer 身份/回退/修订订阅、API URL 构建与错误映射、Center 筛选与未读入口、徽标未读入口、详情页步骤）。

真实 API 联调（Agent 8000 + 既有 Trace，viewer 随机 UUID）：

```text
sessions=5  (全部 trigger=alert, alert=PodCrashLooping/open|closed, code=CONFIG_ERROR|CRASH_LOOP_BACKOFF, unread=True)
notifications unread=5  ->  标记 1 条已读后 unread=4（幂等 200）
unresolved=0
keyset: limit=1 两页 id 不同；同游标换 filter -> 422
timeline diag_04c9cc036694 available=available gap=False items=5（diagnosis_started/running、llm_call、tool_completed）
```

未验证：Headlamp 页面的人工视觉验收（需人工打开 Headlamp 查看 Center/Detail/徽标）；插件与真实后端的 UI 端到端（模拟 webhook → Center 查看 → 已读 → 重启 Agent 持久化）中的 UI 部分。

## 已知限制

- **单实例边界**：会话与未读在 SQLite、Trace 在本地 JSONL；`agent-service/deploy/` 已改为 **PVC（`agent-service-data`）+ `TRACE_DIR=/data/trace`**，需持久卷；仍不可在多副本间共享（勿把本地 JSONL 当共享存储）。
- `viewer_id` 是浏览器本地 UUID，**不是认证/授权/RBAC**；清除 localStorage 会重置未读；跨标签页通过 storage 事件同步。
- SDK 的 `headlamp-plugin test` **不接受 `--run`**（本版本报 `Unknown argument: run`）；单次运行请用
  `node_modules\.bin\vitest.cmd run -c node_modules/@kinvolk/headlamp-plugin/config/vite.config.mjs`。
- 插件页面的人工视觉验收与「关闭 Headlamp → 模拟告警 → 重开查看/标记已读 → 重启 Agent 仍可查」的 UI 闭环仍未执行（后端与该闭环的后端部分已验证）。
