# WeatherQuant Dashboard — Unraid 部署（P2-1）

目标地址：`http://<NAS-LAN-IP>:8787`
镜像：`ghcr.io/aorulola/weatherquant-next-dashboard:dashboard-stable`
回滚目标：`sha-<commit>`（不可变）

本文件与 `docker/compose.dashboard.unraid.yaml` 配套使用。Dashboard 是第三个
独立容器：不改 Collector、不改 Shadow、不需要 Docker Socket、不打开任何
`.duckdb`，崩溃也不影响两个生产服务。

---

## 1. 前置条件

- Collector 栈（`compose.unraid.yaml`）与 Shadow 栈（`compose.shadow.unraid.yaml`）
  已按各自文档部署（可选——缺失时 Dashboard 会以 `missing` 状态解释，而不是崩溃）。
- Compose Manager Plus（或任意 compose 运行方式）。
- 无需 GPU、无需额外数据库、无需任何凭据。

## 2. 数据目录：只挂子目录

Dashboard 只需要四個**只读**子目录：

```text
/mnt/user/appdata/weatherquant-next/health      collector 心跳
/mnt/user/appdata/weatherquant-next/raw         collector 原始归档（有界 tail 读取）
/mnt/user/appdata/weatherquant-shadow/health    shadow 心跳
/mnt/user/appdata/weatherquant-shadow/reports   shadow report 与明细快照
```

首次部署前确保它们存在（不存在则 `mkdir -p`，否则 Docker 会以 root 创建空目录，
虽不影响运行，但会留下所有权不符的目录）：

```bash
mkdir -p /mnt/user/appdata/weatherquant-shadow/reports
```

**不挂载** `weatherquant.duckdb` / `shadow.duckdb` / 整个数据目录。compose 文件里
每条挂载都以 `:ro` 结尾，且只指向 `health|raw|reports`——`tests/test_dashboard_ci.py`
对这一点有不变量测试，改动 compose 会让 CI 失败。

> Shadow 报表快照（`reports/report.json`）由 Shadow 引擎发布，需要运行包含
> report 快照机制的版本：**shadow-v0.1.1（1e7e9d3）起已包含**，shadow-v0.1.0
> 没有。升级前 Dashboard 的 shadow-report 面板显示 `missing`，属设计内降级。

## 3. 部署步骤（Oreochromis）

Compose **默认钉死 SHA**：镜像行是
`...:sha-${DASHBOARD_SHA:?...}`，`.env` 里不给出 commit sha 时 compose 拒绝启动——
浮动标签不会被悄悄拉到生产。

1. 在 Compose Manager Plus 新建 stack（与 Collector、Shadow 并列的独立目录，例如
   `/mnt/user/appdata/plugins/compose-manager/weatherquant-dashboard/`），
   粘贴 `docker/compose.dashboard.unraid.yaml`。
2. **写入发布 SHA**（取自 dashboard-release Actions 产物摘要 / 发布说明，
   即 `sha-<commit>` 中的 `<commit>`）：
   ```bash
   cd /mnt/user/appdata/plugins/compose-manager/weatherquant-dashboard
   echo "DASHBOARD_SHA=<commit-sha>" > .env
   ```
3. （可选，推荐）把端口改成绑定 NAS 局域网地址：
   ```yaml
   ports:
     - "192.168.1.20:8787:8787"    # 换成 Oreochromis 的 LAN IP
   ```
4. 拉取并启动：
   ```bash
   docker pull ghcr.io/aorulola/weatherquant-next-dashboard:sha-<commit-sha>
   docker compose -p weatherquant-dashboard up -d
   # （Compose Manager Plus 则直接点 Apply，效果相同）
   ```
5. 等待容器健康（`start_period 20s`）：
   ```bash
   docker ps --filter name=weatherquant-dashboard
   docker logs weatherquant-dashboard | tail
   ```
5. 浏览器打开 `http://<NAS-LAN-IP>:8787`。

预期首屏：

- 顶部绿色 `Dashboard API 已连接`；
- Collector / Shadow 徽章按真实心跳显示（缺失时显示 `MISSING` 灰色徽章并给出原因）；
- 没有 `FIXTURE` 横幅（有则说明镜像环境被设成了 `WQ_DATASET=fixture`，生产必须 `live`）。

## 4. 健康检查语义

`HEALTHCHECK` 调用 `docker/healthcheck_dashboard.py` → `GET /healthz`：

- **200 = Dashboard 自身在服务**（这就是 Docker 需要知道的全部）；
- Collector/Shadow 的状态在响应体 `data.sources` 与各页面里，**不参与重启判定**——
  上游挂掉时重启 Dashboard 什么也修不好，反而丢掉唯一能解释故障的组件。

## 5. 升级与回滚

```bash
cd /mnt/user/appdata/plugins/compose-manager/weatherquant-dashboard

# 升级：把 .env 换成新发布产物的 SHA → pull → up
echo "DASHBOARD_SHA=<new-commit-sha>" > .env
docker pull ghcr.io/aorulola/weatherquant-next-dashboard:sha-<new-commit-sha>
docker compose -p weatherquant-dashboard up -d

# 回滚：换回旧 SHA → up（旧镜像仍留在本地，秒级回退）
echo "DASHBOARD_SHA=<previous-commit-sha>" > .env
docker compose -p weatherquant-dashboard up -d
```

`dashboard-stable` 只是"最新通过验收的版本"这个**指针**，供追踪使用；
生产 compose 默认不引用它——回滚锚点永远是不可变的 `sha-<commit>`。
Dashboard 没有本地状态（不写任何挂载），升级/回滚不需要迁移。

## 6. 网络与安全边界

- **本版本没有用户认证**：端口只应暴露在可信局域网（或绑定 NAS 的 LAN IP）。
  **禁止**通过路由器端口映射、Tailscale/公网转发等方式把它公开到互联网；
  心跳与市场数据虽非机密，但"无认证 + 公网"不是这个产品的部署形态。
- 无钱包、无交易 API、无出站凭据；容器仅监听 8787，出站无需求。
- 不安装 Docker CLI、不挂 `docker.sock`、非 root（uid 1000）运行、
  `no-new-privileges`、日志轮转 20m×5。
- 如需远程访问，请先在路由器/防火墙层限定来源 IP，或经由带认证的反向代理
  （超出本版本范围）。

## 7. 验证清单（部署后）

```bash
# 1. 健康
docker exec weatherquant-dashboard python /usr/local/bin/healthcheck.py
# 2. 数据源被看到且只读
docker exec weatherquant-dashboard python -c \
  "import urllib.request,json;print(json.load(urllib.request.urlopen('http://127.0.0.1:8787/healthz'))['data']['sources'])"
# 3. 挂载不可写
docker exec weatherquant-dashboard sh -c 'touch /sources/collector/health/x && echo WRITABLE || echo RO-ok'
# 4. 端口
curl -s http://<NAS-LAN-IP>:8787/healthz | head -c 200
```

自动化版本：每次 CI/发布都会运行 `scripts/dashboard_docker_accept.sh`
（启动、非-root、信封、只读、无 .duckdb、SPA、重启恢复、优雅停止共 8 项）。

## 8. 故障排查

| 现象 | 含义 | 处理 |
|---|---|---|
| 面板显示 `collector-heartbeat: missing` | Collector 未运行或 `health/` 未挂载 | 检查 Collector 栈与挂载路径 |
| `shadow-report: missing` | Shadow 未发布 report（版本过旧或未运行） | 升级 Shadow 到含 report 快照的版本 |
| `shadow/reports/*-snapshot.json` 相关面板 `unavailable` | 明细生产者尚未集成 | 见 `dashboard-data-contract.md` §4，待 Shadow 集成 |
| 全部 `stale` | 数据在流动但超过窗口 | 检查两个引擎是否卡死（看各自心跳 age） |
| 页面顶部离线横幅 | Dashboard 后端不可达 | `docker logs weatherquant-dashboard` |
| 429 | 单客户端超过 600 req/min | 自动恢复；如为误伤可调 `WQ_RATE_LIMIT_PER_MINUTE` |

## 9. 明确的非目标

- 不采集行情、不跑模型、不做预测决策、不交易、不模拟成交；
- 不修改策略阈值与模型；不改变 Collector 采样语义；
- 不读写生产 DuckDB；不提供任何写接口或 Shell 接口。
