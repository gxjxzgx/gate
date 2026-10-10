# gatetool-ovpn

VPN Gate OpenVPN 节点自动刷新流水线: 每小时拉取 VPN Gate 数据, 提取 OpenVPN 配置并做 TCP 存活检查,
发布到 GitHub Pages 监控页, Clash 订阅上传到私有 Cloudflare Worker。
`ovpn.py` 与工作流 `ovpn.yml` 同名, 共用 `common.py` (日志、环境变量、HTTP、VPN Gate 解析、节点命名规则、原子写文件),
**只用 Python 标准库, 无需安装依赖**。

| 工作流 | 脚本 | 频率 | 输出 |
|---|---|---|---|
| `ovpn.yml` | `ovpn.py` | 每小时 | `ovpn.json`(Pages, 含全部节点); `ovpn.yaml`(Clash, 私有 Worker; 住宅节点 > 20 个时不含机房节点) |

数据文件只在工作流运行时生成并发布, 不提交到仓库。
每次运行结束清理旧的运行记录, 只保留最新 3 条。

## 节点命名

网页 (`ovpn.json`) 和订阅 (`ovpn.yaml`) 使用同一份名字, 规则: **`地区-类型-序号`**, 例: `日本-住宅-01` `日本-机房-01`。
命名只在 `ovpn.py` 里做一次, 因此同一个节点在网页「名称」列和 Clash 里叫同一个名字。

- 类型只有两种: 住宅 / 机房 (按 VPN Gate 主机名前缀估算, 仅供参考); 未识别节点不加入节点列表。
- 地区用中文国名 (对照表见 `common.py` 的 `COUNTRY_ZH`, 未收录的显示国家码); 国家码不同但中文名相同的
  (如 GB 与 UK 都是"英国") 合并后连续编号, 不会重名。
- 序号在「同一地区 + 同一类型」内从 01 开始。排序: 地区 → 住宅 → 机房 → **延迟从低到高** (UDP 无延迟, 排在同类最后),
  所以 `-01` 就是该地区该类型里延迟最低的节点。
- 订阅里剔除了机房节点, 或设置了 `MAX_YAML` 截断时, 序号会留空 (如只剩 `-01` `-03`), 不会重新编号, 保证和网页对得上。
- 节点每小时都在变化, 序号也会随延迟排名变化, 同一个名字不保证一直指向同一台服务器。
- 修改规则只需改 `common.py` 的 `node_name()` 和 `TYPE_LABEL`。
- 节点名里不使用 emoji / 图标。

## 目录

```
common.py              共用工具
ovpn.py                OpenVPN 节点提取 + TCP 存活检查
tools/prepare_site.sh  创建 site/ 并放入监控页
tools/upload_private.sh  把私有订阅上传到 Worker, 并确保不留在站点目录
web/index.html         监控页
worker/                私有订阅托管 Worker
.github/workflows/     ovpn.yml
```

## 站点

`https://<用户>.github.io/<仓库>/` 打开 `index.html`, 显示 OpenVPN 节点列表: 按国家分组, 组内先住宅后机房、同类按延迟从低到高。
表格列为 名称 / 地址 / 协议 / 延迟 / 类型, 点击名称或地址可复制。
页面读取同目录的 `ovpn.json`, 每 5 分钟自动刷新, 刷新时会保留你已展开的国家分组。
国家的中文名由 `ovpn.json` 提供 (来自 `common.py`), 页面里不再单独维护一份对照表。

## 部署

1. Settings → Actions → General → Workflow permissions 选 **Read and write permissions**。
2. 在 GitHub 网页上手动创建 `.github/workflows/ovpn.yml` (API 推不了 workflows 文件)。
3. Actions 页手动运行 `OpenVPN Refresh` 一次。

## 环境变量

空字符串视为未设置 (GitHub 里没配的 secret 就是空字符串)。

| 变量 | 默认 | 说明 |
|---|---|---|
| `OUT_DIR` | `site` | 输出目录 |
| `WORKERS` | 32 | 并发数 |
| `TIMEOUT` | 5 | 单节点 TCP 超时秒数 |
| `KEEP_UDP` | 1 | UDP 节点: 1=不检查直接保留, 0=丢弃 (工作流里设为 0) |
| `MAX_YAML` | 0 | ovpn.yaml 最多保留 N 个, 0=全部 (保留延迟最低的 N 个; 网页仍显示全部) |
| `EXCLUDE_DC` / `MIN_ISP` | 1 / 20 | 住宅节点超过阈值时, 订阅里剔除机房节点 |
| `INCLUDE_COUNTRIES` | — | 国家白名单, 逗号分隔的国家码 (如 `JP,KR`), 同时作用于订阅和网页, 为空=不过滤。先过滤再做 TCP 检查, 白名单外的节点不会被探测 |
| `VPNGATE_API` / `VPNGATE_MIRROR` | 官方 / GitHub 镜像 | 数据源 |

本地试跑示例: `OUT_DIR=/tmp/site python ovpn.py`

## 失败保护

**宁可失败, 也不用空结果覆盖线上旧数据**。

- 数据源全挂、解析不出节点、检测后没有任何可用节点 → 退出码 1, 工作流停止, 不上传不部署。
- 文件先写临时文件再原子替换, 中途失败不留下半截文件。
- 数据文件每次都由 `ovpn.py` 完整重新生成, 失败时工作流在部署前就停了, 所以线上旧数据始终保持原样, 不需要也不会取回旧文件再重新部署。
- 私有订阅上传失败 → 工作流报错且不部署; 上传后再次确认站点目录里没有私有文件。

---

## 订阅放私有 Worker (不进 Pages)

`ovpn.yaml` 由工作流上传到你自己的 Cloudflare Worker (KV), 站点上不发布。

1. Cloudflare 控制台: 新建 KV 命名空间, 记下 id, 填进 `worker/wrangler.toml`
   (或在 Worker 设置里绑定, 绑定名必须是 `SUBS`)。`wrangler.toml` 里的 `PAGES_URL` 默认是注释掉的, 见第 5 步。
2. 部署 `worker/worker.js` 为一个 Worker (控制台粘贴代码即可), 并绑定自定义域名
   (国内直连 workers.dev 常常不通, 建议用你自己的域名)。
3. 在 Worker 设置里添加一个 **Secret**: `ACCESS_TOKEN`, 用 `openssl rand -hex 16` 生成。
   它同时是上传密钥 (`Authorization: Bearer`) 和订阅访问令牌 (URL 路径)。**必须配置**, 未配置时上传会被拒绝 (HTTP 500)。
4. 仓库 Secrets 添加: `WORKER_URL` (如 `https://sub.example.com`)、`ACCESS_TOKEN` (同上)。
5. (可选) 让 Worker 主域名直接显示监控页: 在 Worker 设置 → Variables 添加普通变量 `PAGES_URL`
   (即 Pages 站点根地址, 不带末尾 `/`); 用 wrangler 部署的话, 取消 `wrangler.toml` 里 `[vars]` 的注释并填好地址
   (别带着占位符部署, 否则主页会 502)。之后 `<WORKER_URL>/` 就是监控页, 并转发 `ovpn.json`。
6. 手动运行一次工作流。订阅地址: `<WORKER_URL>/<ACCESS_TOKEN>/ovpn.yaml`。

上传失败时工作流会报错并且不部署, 旧数据保持不变。
