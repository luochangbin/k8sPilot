# ai-diagnosis-plugin

Headlamp 插件（Phase 1）：在 **Pod 详情页**提供「智能诊断」入口，点击后向 k8sPilot Agent Service 提交诊断请求，轮询 Diagnosis Session，并展示结构化诊断结果（症状、调查过程、Root Cause、证据、置信度、修复建议）。

设计参考：`K8S管理平台智能诊断系统设计.md` §22。

## 功能

- Pod 详情页新增「智能诊断」区块。
- 点击按钮 → `POST {AGENT_BASE_URL}/api/v1/diagnoses`，携带完整资源身份（`kind/namespace/name/uid`）。
- 每 2 秒轮询 `GET /api/v1/diagnoses/{id}`，实时展示调查进度。
- 结果结构化展示；证据不足时显示 warning 与缺失证据清单；失败时显示错误。

## 配置 Agent Service 地址

默认 `http://localhost:8000`。覆盖方式（二选一）：

1. 运行时注入：在 Headlamp 加载插件前设置全局变量
   ```html
   <script>
     window.__K8S_PILOT_AGENT_BASE__ = 'http://agent-service.k8spilot.svc.cluster.local:8000';
   </script>
   ```
2. 编辑 `src/DiagnosisSection.tsx` 顶部的 `AGENT_BASE_URL` 常量后重新构建。

## 开发

```bash
npm install
npm start        # 启动 Headlamp 并热加载插件
```

## 构建

```bash
npm run build    # 产物输出到 dist/
```

## 挂载到 Headlamp

- 本地 dev：`npm start` 会自动加载。
- 部署态：将 `dist/` 作为 Headlamp 插件目录挂载，或用 `npm run package` 打包后放入 Headlamp 插件目录：
  ```
  headlamp server --plugins-dir <path>/ai-diagnosis-plugin/dist
  ```
  集群内部署时用 ConfigMap/持久卷挂载 `dist/` 到 Headlamp 的插件目录，并设置 `window.__K8S_PILOT_AGENT_BASE__` 指向 Agent Service。

## 注意

- 浏览器直连 Agent Service，需要其可达（本机 `localhost:8000` 或集群内 Headlamp 同网络可达的 Service）。
- Phase 1 不提供聊天、追问、告警入口或修复操作。
