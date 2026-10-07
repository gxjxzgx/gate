# gatetool-ovpn

VPN Gate OpenVPN 节点自动刷新流水线: 每小时拉取 VPN Gate 数据, 提取 OpenVPN 配置并做 TCP 存活检查,
发布到 GitHub Pages 监控页, Clash 订阅上传到私有 Cloudflare Worker。
`ovpn.py` 与工作流 `ovpn.yml` 同名, 共用 `common.py` (日志、环境变量、HTTP、VPN Gate 解析、原子写文件),
**只用 Python 标准库, 无需安装依赖**。

| 工作流 | 脚本 | 频率 | 输出 |
|---|---|---|---|
| `ovpn.yml` | `ovpn.py` | 每小时 | `ovpn.json`(Pages, 含全部节点); `ovpn.yaml`(Clash, 私有 Worker; 住宅节点 > 20 个时不含机房节点) |

数据文件只在工作流运行时生成并发布, 不提交到仓库。

## 目录

```
common.py              共用工具 (含 PUBLIC_FILES / PRIVATE_FILES 文件清单, 单一来源)
ovpn.py                OpenVPN 节点提取 + TCP 存活检查
tools/prepare_site.sh  从线上取回旧数据文件 (取回后会校验内容)
tools/upload_private.sh  把私有订阅上传到 Worker, 并确保不留在站点目录
web/index.html         监控页
worker/                私有订阅托管 Worker
.github/workflows/     ovpn.yml
```

## 站点

`https://<用户>.github.io/<仓库>/` 打开 `index.html`, 显示 OpenVPN 节点列表 (住宅 → 机房 → 未识别 排序)。
页面读取同目录的 `ovpn.json`。页面每 5 分钟自动刷新, 刷新时会保留你已展开的国家分组。

## 部署

1. Settings → Actions → General → Workflow permissions 选 **Read and write permissions**。
2. 在 GitHub 网页上手动创建 `.github/workflows/ovpn.yml` (API 推不了 workflows 文件)。
3. Actions 页手动运行 `OVPN Check` 一次。

## 环境变量

空字符串视为未设置 (GitHub 里没配的 secret 就是空字符串)。

| 变量 | 默认 | 说明 |
|---|---|---|
| `OUT_DIR` | `site` | 输出目录 |
| `WORKERS` | 32 | 并发数 |
| `TIMEOUT` | 5 | 单节点 TCP 超时秒数 |
| `KEEP_UDP` | 1 | UDP 节点: 1=不检查直接保留, 0=丢弃 (工作流里设为 0) |
| `MAX_YAML` | 0 | ovpn.yaml 最多保留 N 个, 0=全部 (按延迟优先截断; 网页仍显示全部) |
| `EXCLUDE_DC` / `MIN_ISP` | 1 / 20 | 住宅节点超过阈值时, 订阅里剔除机房节点 |
| `VPNGATE_API` / `VPNGATE_MIRROR` | 官方 / GitHub 镜像 | 数据源 |

本地试跑示例: `OUT_DIR=/tmp/site python ovpn.py`

## 失败保护

**宁可失败, 也不用空结果覆盖线上旧数据**。

- 数据源全挂、解析不出节点、检测后没有任何可用节点 → 退出码 1, 工作流停止, 不上传不部署。
- 文件先写临时文件再原子替换, 中途失败不留下半截文件。
- 私有订阅上传失败 → 工作流报错且不部署; 上传后再次确认站点目录里没有私有文件。

---

## 订阅放私有 Worker (不进 Pages)

`ovpn.yaml` 由工作流上传到你自己的 Cloudflare Worker (KV), 站点上不发布。

1. Cloudflare 控制台: 新建 KV 命名空间, 记下 id, 填进 `worker/wrangler.toml`
   (或在 Worker 设置里绑定, 绑定名必须是 `SUBS`)。
2. 部署 `worker/worker.js` 为一个 Worker (控制台粘贴代码即可), 并绑定自定义域名
   (国内直连 workers.dev 常常不通, 建议用你自己的域名)。
3. 在 Worker 设置里添加一个 **Secret**: `ACCESS_TOKEN`, 用 `openssl rand -hex 16` 生成。
   它同时是上传密钥 (`Authorization: Bearer`) 和订阅访问令牌 (URL 路径)。**必须配置**, 未配置时上传会被拒绝 (HTTP 500)。
4. 仓库 Secrets 添加: `WORKER_URL` (如 `https://sub.example.com`)、`ACCESS_TOKEN` (同上)。
5. (可选) 让 Worker 主域名直接显示监控页: 在 Worker 设置 → Variables 添加普通变量 `PAGES_URL`
   (即 Pages 站点根地址, 不带末尾 `/`)。之后 `<WORKER_URL>/` 就是监控页, 并转发 `ovpn.json`。
6. 手动运行一次工作流。订阅地址: `<WORKER_URL>/<ACCESS_TOKEN>/ovpn.yaml`。

上传失败时工作流会报错并且不部署, 旧数据保持不变。
