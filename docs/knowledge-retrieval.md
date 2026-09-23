# 知识库与检索指南

本文说明如何在本地启用运维知识检索、导入 Markdown/PDF 与历史 Incident，并了解检索边界。知识库是可选能力；未配置 PostgreSQL 或旧 SQLite 知识库时，Agent Service 仍可独立运行。

## 支持内容与当前实现

- Markdown (`.md`)：正文必须使用 UTF-8，并以 YAML frontmatter 描述类型、标题、适用产品/版本/环境/资源等元数据。
- PDF (`.pdf`)：必须与 `<文件名>.metadata.yaml` 同目录、同主文件名。只支持可提取文本的 PDF；扫描图片或其他无可提取文本内容的页面会拒绝导入，当前不执行 OCR。纯空白页会跳过并报告页码；最多 500 页、单文件最多 20 MiB。
- Incident：以 Markdown frontmatter 表示，不接受任意 YAML 文件作为文档。`draft` 可保存但不能作为历史案例返回；只有 `verified` 且包含 `root_cause_code` 和 `verification` 的案例才可检索。
- 旧 SQLite seeds 模式仍可使用 `KNOWLEDGE_DB`；文件导入走 PostgreSQL，不会自动迁移旧 SQLite 数据，也不会扫描未显式指定目录之外的文件。

知识文档 chunk 使用本地 Embedding tokenizer 分段，目标不超过 480 tokens（并有 4,000 字符预切块上限）；PDF chunk 保留原始页码。知识 chunk 使用 PostgreSQL/pgvector cosine 距离与 PostgreSQL 词项检索，经 RRF 合并；中文词项检索通过显式单字/双字词项构建，不依赖 PostgreSQL `simple` 配置自动切词。Incident 目前只做词项检索，并强制过滤为 `verified`。

向量和原始知识内容存储于本地配置的 PostgreSQL 数据库。Embedding 在本机通过 FastEmbed 生成，不发送到 Embedding API。Agent 工具在知识库配置后默认可用，但不是每次诊断都必须调用；实时 Kubernetes/指标/日志证据仍优先。检索结果的实际片段会进入现有 Agent 提示，可能随诊断上下文发送到所配置的外部 LLM。请避免导入凭据、个人数据或未获准发送给模型服务的内容。

当前实现面向单用户/单服务配置，不是多租户权限系统。带非空 `acl_tags` 的文档在没有服务端可信 ACL allow-list 时不会进入搜索结果；调用方或 LLM 不能通过检索参数自行授予权限。应按单一受信服务边界部署，并自行限制数据库、Agent API 和资料目录访问。

## 准备本地 PostgreSQL

本地开发可使用 pg0。pg0 CLI 二进制不包含在 Git 仓库中；本次 Windows x64 本地下载的是 [pg0 v0.15.2 官方 Release](https://github.com/vectorize-io/pg0/releases/tag/v0.15.2)，本机文件放在 `PostgreSQL/bin/`（忽略、不提交）。其他开发机请从该官方 Release 获取对应平台的 CLI。

```powershell
# 在仓库根目录运行；实例数据写入项目 PostgreSQL/data
& .\PostgreSQL\bin\pg0-windows-x86_64.exe start `
  --name k8spilot-knowledge `
  --data-dir 'D:\AI\k8sPilot\PostgreSQL\data' `
  --port 5432 `
  --config listen_addresses=127.0.0.1

# 确保该 PostgreSQL 安装可用 pgvector 扩展
& .\PostgreSQL\bin\pg0-windows-x86_64.exe install-extension vector --name k8spilot-knowledge
```

pg0 默认将 PostgreSQL 运行时安装与缓存放在当前 Windows 用户的 `%USERPROFILE%\.pg0`（例如 `C:\Users\<user>\.pg0`）；这不表示所有项目组件都安装在该目录。项目目录中的 `PostgreSQL/data` 是数据库实例数据，`PostgreSQL/models` 是 Embedding 模型缓存。以上 `start` 命令创建/启动 `k8spilot-knowledge` 实例，停止同一实例：

```powershell
& .\PostgreSQL\bin\pg0-windows-x86_64.exe stop --name k8spilot-knowledge
```

服务器应仅监听本机开发接口。生产部署请按组织标准单独管理 PostgreSQL、网络、备份和凭据，不要将本地 pg0 命令当作生产部署方案。

为 Agent 创建专用数据库和最小权限角色，并确保 `vector` 扩展可在目标数据库使用。首次初始化会创建配置表、知识表和索引；配置的 schema 可由管理员预先创建并授予 Agent 角色 `USAGE`、`CREATE`。若 schema 不存在，Agent 会尝试创建；角色缺少权限时会报错并提示由管理员预置，不需要为应用角色授予数据库级 `CREATE`。不要把管理员密码写入 README、脚本或提交到 Git。

## 安装与配置 Agent Service

在仓库根目录执行：

```powershell
cd agent-service
if (-not (Test-Path .env)) { Copy-Item .env.template .env }
if (-not (Test-Path .venv\Scripts\python.exe)) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -e ".[knowledge]"
```

编辑 `agent-service/.env`，按实际数据库替换占位符；此文件包含秘密，不应提交：

```dotenv
KNOWLEDGE_DATABASE_URL=postgresql://<db-user>:<password>@127.0.0.1:5432/<database>
KNOWLEDGE_DATABASE_SCHEMA=k8spilot_knowledge
KNOWLEDGE_EMBEDDING_MODEL=minishlab/potion-multilingual-128M
KNOWLEDGE_EMBEDDING_CACHE_DIR=PostgreSQL/models
```

相对缓存目录按仓库根目录解析。pg0 的 PostgreSQL 运行时安装放在 `%USERPROFILE%\.pg0`，模型文件放在项目 `PostgreSQL/models`。首次构造本地 Embedding provider 会下载模型；当前模型权重约 512 MB，缓存目录总量约 547 MB；实际进程内存占用尚未测量。若模型已缓存则无需重复下载。数据库中会固定 Embedding model ID 和维数；更换模型/维数必须另做迁移或新建知识库，不能混写。

重启 Agent Service 后，启动日志若显示知识库初始化失败会降级为未启用；服务不会把带凭据的 DSN 写入日志。健康检查通过后，可用下面命令列出已导入记录：

```powershell
.\.venv\Scripts\python.exe -m app.knowledge.ingest --list
```

## Markdown 文档格式

最小 Runbook 示例：

```markdown
---
document_id: runbook-pod-oom-v1
source_type: runbook
title: Pod OOMKilled triage
product: kubernetes
versions:
  - "1.30"
environments:
  - production
resource_kinds:
  - Pod
status: active
source_uri: https://docs.example.org/runbooks/pod-oom
---

# Symptoms

Describe the observable symptoms and verified troubleshooting steps.
```

`document_id` 建议显式提供稳定 ID；若省略会由文件路径生成，移动文件会改变 ID 并产生新记录。`source_type` 必须是 `runbook`、`product_doc`、`known_issue` 或 `sop`。`source_uri` 若填写必须使用 HTTP(S)；省略时引用会使用文件名。字段和完整示例见 [`docs/knowledge-examples/`](knowledge-examples/)。

PDF sidecar 例如 `kubernetes-guide.metadata.yaml`（与 `kubernetes-guide.pdf` 同目录）：

```yaml
source_type: product_doc
title: Kubernetes user guide
product: kubernetes
versions:
  - "1.30"
resource_kinds:
  - Pod
source_uri: https://kubernetes.io/docs/
```

扫描型 PDF 需先由用户在来源侧转换为可检索文本或提供可访问的文本版；当前导入器不会 OCR，也不会联网抓取 `source_uri`。

## 导入、增量更新和删除

```powershell
# 从 agent-service 目录执行，支持单文件或递归目录
.\.venv\Scripts\python.exe -m app.knowledge.ingest --import ..\docs\knowledge-examples

# 查看知识文档和 Incident
.\.venv\Scripts\python.exe -m app.knowledge.ingest --list

# 按精确 ID 删除一条文档或 Incident
.\.venv\Scripts\python.exe -m app.knowledge.ingest --delete <exact-id>
```

导入器只扫描 `.md` 和 `.pdf`；忽略目录中的其他文件，不跟随符号链接。Markdown 及 PDF sidecar 元数据参与 checksum。未变化且 checksum、Embedding model ID 与维数一致的记录会跳过 Embedding 和写入；变更时同一 ID 的文档及 chunks 在单事务中替换，导入或 Embedding 失败会保留旧版本。目录扫描不会推断删除：源目录里消失的文件仍留在索引中，需使用 `--delete` 显式删除。命令会列出 imported/skipped/failed；只要存在 failed，退出码非零。

示例目录包括 `runbook-pod-oom.md`、供 `product-guide.pdf` 使用的 `product-guide.metadata.yaml` sidecar 模板（PDF 文件本身不随仓库提供）、`incident-draft.md` 和 `incident-verified.md`。其中演示事件和产品名称是样例，不是项目自带的真实生产知识。

## 旧 SQLite seeds

旧路径仍可创建或查看 SQLite FTS5 seeds：

```powershell
.\.venv\Scripts\python.exe -m app.knowledge.ingest --db ..\data\knowledge.db --show
```

若同时配置了 `KNOWLEDGE_DATABASE_URL` 和 `KNOWLEDGE_DB`，PostgreSQL 优先。SQLite seed 模式不包含 MD/PDF 文件导入，也不会自动迁移到 PostgreSQL。需要复用旧 seeds 时，应单独验证 schema 与业务数据后做显式迁移。

## 引用与检索限制

- Agent 只允许把实际检索返回的 `retrieval_id` 作为引用；知识引用携带文档 ID、标题、section、版本、更新时间，以及 PDF 的起止页码（如有）。
- 每条知识片段最多 600 字符，单次工具结果总计最多 3,000 字符（截断后缀也计入上限）。这些内容仍会进入当前 Agent/LLM 请求。
- 知识和历史 Incident 用来辅助假设、调查路径和建议，不属于实时 Evidence，也不能单独证明 Root Cause。实时工具证据与知识冲突时，以当前环境证据为准。
- PostgreSQL 文档检索通过精确 cosine 检索和 lexical rank 进行 RRF；Incident 只有 verified 过滤和词项匹配，目前没有 Incident 向量索引。
- 数据库连接使用有限连接/语句超时。数据库离线时知识检索初始化可能降级为关闭；诊断数据库、知识库数据库是不同用途，不要共用其数据目录。

## Opt-in PostgreSQL 集成验证

以下集成测试默认不会连接数据库。需要先确认 `agent-service/.env` 中的 `KNOWLEDGE_DATABASE_URL` 指向专用测试数据库，并且 `k8spilot_knowledge` 是已预置的非 `public` schema；测试不会申请额外授权或创建/删除 schema。测试只写入唯一 UUID 测试记录，并在结束时精确清理。

```powershell
# 在 agent-service 目录执行；未配置知识库依赖时请先安装项目 knowledge extra
$env:K8SPILOT_TEST_SCHEMA='k8spilot_knowledge'
.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_embeddings.py tests/test_knowledge_import_integration.py
Remove-Item Env:K8SPILOT_TEST_SCHEMA
```

不设置 `K8SPILOT_TEST_SCHEMA` 时，PostgreSQL 集成测试会跳过；不要将该变量设置为 `public` 或生产 schema。
