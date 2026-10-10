#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ovpn.py —— VPN Gate OpenVPN 节点提取 + TCP 存活检查 (工作流: ovpn.yml)

流程:
  1. 拉取 VPN Gate 数据 (官方 CSV, 失败时回退 GitHub 镜像)
  2. 解码每个服务器的 OpenVPN 配置, 提取 remote 地址 / 端口 / 协议 (只接受公网地址)
     并按 INCLUDE_COUNTRIES 做国家白名单过滤 (先过滤再检查, 省掉无用的连接)
  3. 并发 TCP 连通检查; UDP 无法用 TCP 探测, 按 KEEP_UDP 保留或丢弃
  4. 排序 (地区 → 住宅 > 机房 → 延迟从低到高) 并统一命名, 输出到 OUT_DIR:
       ovpn.json   网页数据 (公开, 含全部可用节点)
       ovpn.yaml   Clash 订阅 (私有, 上传到 Worker)
     两个文件里同一节点的名字完全一致 (地区-类型-序号)。
     住宅节点超过 MIN_ISP 个时, 机房节点只留在 ovpn.json, 不进 ovpn.yaml。

退出码: 0 正常; 1 硬性失败 (数据源全挂 / 没有提取到节点 / 全部不可达 / 程序异常)。
        任何情况都不会用空结果覆盖线上旧数据。

环境变量:
  WORKERS / TIMEOUT   检测并发 (默认 32) / 单节点 TCP 超时秒数 (默认 5)
  KEEP_UDP            UDP 节点: 1=不检查直接保留, 0=丢弃 (默认 1; 工作流里设为 0)
  MAX_YAML            ovpn.yaml 最多保留 N 个, 0=全部 (按延迟优先截断; 网页仍显示全部)
  EXCLUDE_DC / MIN_ISP  机房节点排除开关 (默认 1) / 住宅数量阈值 (默认 20)
  INCLUDE_COUNTRIES   国家白名单, 逗号分隔的国家码 (如 JP,KR), 同时作用于订阅和网页, 为空=不过滤
  OUT_DIR             输出目录
  VPNGATE_API / VPNGATE_MIRROR   数据源地址
"""

import os
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor

from common import (
    MIN_ISP, OUT_DIR, classify_host, country_label, decode_config, drop_datacenter, env_flag, env_float,
    env_int, env_str, fetch_vpngate_rows, is_public_host, make_logger, node_name, now_bj, parse_remote,
    type_rank, write_json, write_text, yaml_str,
)

log, die = make_logger("ovpn")

# ---------------------------------------------------------------- 配置
WORKERS = max(1, env_int("WORKERS", 32))
TIMEOUT = env_float("TIMEOUT", 5)
MAX_YAML = env_int("MAX_YAML", 0)
KEEP_UDP = env_flag("KEEP_UDP", True)
# 国家白名单: 逗号分隔的国家码 (如 JP,KR), 同时作用于 ovpn.yaml 订阅和网页; 为空=不过滤
INCLUDE_COUNTRIES = [c.strip().upper() for c in env_str("INCLUDE_COUNTRIES").split(",") if c.strip()]

# 配置内容来自第三方, 写入 YAML 前必须校验
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


# ---------------------------------------------------------------- 提取
def extract_nodes(rows):
    nodes, seen = [], set()
    for r in rows:
        cfg = decode_config(r["config_b64"])
        remote = parse_remote(cfg)
        if not remote:
            continue
        host, port, proto = remote
        if not is_public_host(host):
            continue
        key = (host.lower(), port, proto)
        if key in seen:
            continue
        seen.add(key)
        nodes.append({
            "country_long": r["country_long"],
            "country_short": r["country_short"],
            "remote_host": host,
            "remote_port": port,
            "proto": proto,
            "ip_type": classify_host(r["host"]),
            "latency_ms": None,
            "config": cfg,
        })
    return nodes


# ---------------------------------------------------------------- 检查
def tcp_latency(node):
    """TCP 连接耗时 (毫秒), 连不上返回 None。"""
    t0 = time.monotonic()
    try:
        with socket.create_connection((node["remote_host"], node["remote_port"]), timeout=TIMEOUT):
            return round((time.monotonic() - t0) * 1000)
    except OSError:
        return None


def check_nodes(nodes):
    """TCP 节点做连接检查; UDP 节点按 KEEP_UDP 直接保留或丢弃。"""
    tcp_nodes = [n for n in nodes if n["proto"] == "tcp"]
    alive = [] if not KEEP_UDP else [n for n in nodes if n["proto"] == "udp"]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for node, ms in zip(tcp_nodes, pool.map(tcp_latency, tcp_nodes)):
            if ms is not None:
                node["latency_ms"] = ms
                alive.append(node)
    return alive


def region_of(node):
    return country_label(node["country_short"], node["country_long"])


def sort_nodes(nodes):
    """地区 → 住宅 > 机房 → 延迟从低到高 (UDP 无延迟排最后) → 地址。
    按中文地区名而不是国家码排, 这样 GB / UK 这类同名地区会排在一起, 序号才连续。"""
    return sorted(nodes, key=lambda n: (
        region_of(n), type_rank(n["ip_type"]),
        n["latency_ms"] is None, n["latency_ms"] or 0,
        n["remote_host"], n["remote_port"],
    ))


def assign_names(nodes):
    """给已排好序的节点统一命名 (地区-类型-序号), 序号在同地区同类型内从 01 起。
    网页和订阅共用这一份名字, 订阅里剔除节点后序号会留空, 但不会和网页对不上。"""
    counters = {}
    for n in nodes:
        n["region"] = region_of(n)
        group = (n["region"], n["ip_type"])
        counters[group] = counters.get(group, 0) + 1
        n["name"] = node_name(n["region"], n["ip_type"], counters[group])


# ---------------------------------------------------------------- 输出
def pem_block(cfg, tag):
    m = re.search(rf"<{tag}>(.*?)</{tag}>", cfg, re.S)
    return m.group(1).strip() if m else ""


def cfg_directive(cfg, name, default):
    m = re.search(rf"^{name}\s+(\S+)", cfg, re.M)
    value = m.group(1) if m else default
    return value if TOKEN_RE.match(value) else default


def skipped_lines(dc_dropped, truncated):
    """「未写入本文件」说明行: 没有被排除的节点时不输出。"""
    if not (dc_dropped or truncated):
        return []
    reasons = []
    if dc_dropped:
        reasons.append(f"机房 {dc_dropped}, 住宅节点超过 {MIN_ISP} 个时机房只在网页显示")
    if truncated:
        reasons.append(f"超出 MAX_YAML={MAX_YAML} 上限 {truncated} 个")
    return [f"# 未写入本文件: {dc_dropped + truncated} 个 ({'; '.join(reasons)})"]


def build_header(source, nodes, dc_dropped=0, truncated=0):
    """订阅文件头部注释: 更新时间 / 数据来源 / 节点统计 / 使用提示。
    dc_dropped: 因「住宅够多」而没写入的机房节点数; truncated: 因 MAX_YAML 上限而没写入的节点数。"""
    isp = sum(1 for n in nodes if n["ip_type"] == "residential")
    dc = sum(1 for n in nodes if n["ip_type"] == "datacenter")
    regions = {}
    for n in nodes:
        regions[n["region"]] = regions.get(n["region"], 0) + 1
    region_text = " ".join(f"{k}{v}" for k, v in sorted(regions.items(), key=lambda kv: -kv[1]))
    lines = [
        f"# 自动更新: {now_bj('%Y-%m-%d %H:%M:%S')} (每小时重新检测)",
        "#",
        "# 数据来源: VPN Gate (筑波大学公益项目) 公开的志愿者 OpenVPN 服务器列表",
        "#   官方 API: http://www.vpngate.net/api/iphone/",
        f"#   本次实际使用: {source}",
        "#   节点由志愿者提供, 随时可能下线; 本文件由 GitHub Actions 定时生成",
        "#",
        f"# 本文件节点: {len(nodes)} 个 (住宅 {isp} / 机房 {dc})",
        *skipped_lines(dc_dropped, truncated),
        f"# 地区分布: {region_text}",
        "# 命名规则: 地区-类型-序号 (住宅/机房按主机名前缀估算, 仅供参考)",
        "# 检测方式: 仅做 TCP 连通检查, 在 GitHub 机房测得, 不代表你本地线路可用" + ("" if KEEP_UDP else "; 已丢弃 UDP 节点"),
        "#",
        "# 使用提示:",
        "#   1. 节点用户名/密码均为 vpn, 证书为 VPN Gate 通用证书 (已用 YAML 锚点共用)",
        "#   2. 延迟高 / 握手超时 / 直连不稳时, 建议使用链式代理 (前置代理):",
        "#      先通过一个稳定的代理节点, 再连接本文件中的 OpenVPN 节点",
        "#      Mihomo 示例: 在节点下添加  dialer-proxy: 你的前置代理或代理组名",
        "#      (需要所用内核/客户端支持 dialer-proxy, 且仅 TCP 节点可链式)",
        "#   3. 住宅节点通常比机房节点更不易被识别, 优先选择住宅节点",
        "#   4. 节点频繁失效属正常现象, 订阅每小时自动刷新",
        "",
    ]
    return "\n".join(lines) + "\n"


def build_clash_yaml(nodes):
    """Clash proxies 列表 (名字取 n["name"])。证书全网通用: 取第一个带完整证书的节点, 用 YAML 锚点定义, 其余引用。"""
    for n in nodes:
        ca, cert, key = (pem_block(n["config"], t) for t in ("ca", "cert", "key"))
        if ca and cert and key:
            break
    else:
        die("所有节点都缺少 ca/cert/key, 拒绝生成 ovpn.yaml")

    def indented(pem):
        return "\n".join("      " + ln for ln in pem.splitlines())

    out = ["proxies:"]
    for i, n in enumerate(nodes):
        cfg = n["config"]
        out += [
            f"  - name: {yaml_str(n['name'])}",
            "    type: openvpn",
            f"    server: {yaml_str(n['remote_host'])}",
            f"    port: {n['remote_port']}",
            f"    proto: {n['proto']}",
            "    username: vpn",
            "    password: vpn",
            f"    cipher: {cfg_directive(cfg, 'cipher', 'AES-128-CBC')}",
            f"    auth: {cfg_directive(cfg, 'auth', 'SHA1')}",
            f"    udp: {'true' if n['proto'] == 'udp' else 'false'}",
            "    handshake-timeout: 30",
            "    remote-dns-resolve: true",
            "    dns: [ 8.8.8.8, 1.1.1.1 ]",
        ]
        if i == 0:
            out += ["    ca: &jkca |-", indented(ca),
                    "    cert: &jkcert |-", indented(cert),
                    "    key: &jkkey |-", indented(key)]
        else:
            out += ["    ca: *jkca", "    cert: *jkcert", "    key: *jkkey"]
    return "\n".join(out) + "\n"


def build_json(nodes, checked):
    regions = {}
    for n in nodes:
        grp = regions.setdefault(n["region"], {"code": n["country_short"], "count": 0})
        grp["count"] += 1
    return {
        "updated_at": now_bj(),
        "total": len(nodes),
        "checked": checked,
        "countries": regions,   # 以中文地区名为键 (GB / UK 已合并)
        "entries": [{
            "name": n["name"],
            "region": n["region"],
            "country_long": n["country_long"],
            "country_short": n["country_short"],
            "host": n["remote_host"],
            "port": n["remote_port"],
            "proto": n["proto"],
            "latency_ms": n["latency_ms"],
            "ip_type": n["ip_type"],
        } for n in nodes],
    }


# ---------------------------------------------------------------- main
def main():
    log("== 1/4 拉取 VPN Gate 数据 ==")
    try:
        rows, source = fetch_vpngate_rows(log)
    except RuntimeError as exc:
        die(f"所有数据源都不可用: {exc}")

    log("== 2/4 提取 OpenVPN 配置 ==")
    nodes = extract_nodes(rows)
    unknown_n = sum(1 for n in nodes if n["ip_type"] == "unknown")
    if unknown_n:
        log(f"剔除 {unknown_n} 个未识别节点 (不加入节点列表)")
        nodes = [n for n in nodes if n["ip_type"] != "unknown"]
    log(f"提取到 {len(nodes)} 个公网节点")
    if not nodes:
        die("没有提取到任何 OpenVPN 节点, 拒绝提交空结果")

    if INCLUDE_COUNTRIES:
        before = len(nodes)
        nodes = [n for n in nodes if (n["country_short"] or "").strip().upper() in INCLUDE_COUNTRIES]
        log(f"国家白名单 {','.join(INCLUDE_COUNTRIES)}: {before} -> {len(nodes)}")
        if not nodes:
            die("国家白名单过滤后无可用节点 (本轮数据源无白名单国家)")

    log(f"== 3/4 TCP 可达检查 (超时 {TIMEOUT:g}s, 并发 {WORKERS}) ==")
    alive = check_nodes(nodes)
    log(f"保留 {len(alive)}/{len(nodes)}" + (" (含未检查的 UDP 节点)" if KEEP_UDP else ""))
    if not alive:
        die("检查后剩余 0 个可用节点, 拒绝提交空结果")

    log("== 4/4 生成输出文件 ==")
    alive = sort_nodes(alive)
    assign_names(alive)   # 命名只做一次, 网页和订阅共用

    isp_n = sum(1 for n in alive if n["ip_type"] == "residential")
    dc_n = sum(1 for n in alive if n["ip_type"] == "datacenter")
    yaml_nodes = alive
    dc_dropped = truncated = 0   # 没写入订阅的节点数, 写进订阅头部说明
    if drop_datacenter(isp_n):
        yaml_nodes = [n for n in alive if n["ip_type"] != "datacenter"]
        dc_dropped = len(alive) - len(yaml_nodes)
        log(f"家宽 {isp_n} 个, 机房 {dc_n} 个只在网页显示, 不写入 ovpn.yaml (写入 {len(yaml_nodes)} 个)")
    else:
        log(f"家宽 {isp_n} 个 (未超过阈值或未启用排除), 机房 {dc_n} 个一并写入")
    if MAX_YAML > 0 and len(yaml_nodes) > MAX_YAML:
        # 只截断订阅: 延迟优先 (UDP 无延迟排最后); 截断后仍保持 alive 的排序
        truncated = len(yaml_nodes) - MAX_YAML
        keep = sorted(yaml_nodes, key=lambda n: (n["latency_ms"] is None, n["latency_ms"] or 0))[:MAX_YAML]
        keep_ids = {id(n) for n in keep}
        yaml_nodes = [n for n in alive if id(n) in keep_ids]

    yaml_path = os.path.join(OUT_DIR, "ovpn.yaml")
    json_path = os.path.join(OUT_DIR, "ovpn.json")
    write_text(yaml_path, build_header(source, yaml_nodes, dc_dropped, truncated) + build_clash_yaml(yaml_nodes))
    write_json(json_path, build_json(alive, checked=len(nodes)))
    log(f"生成 {yaml_path} ({len(yaml_nodes)} 个节点)")
    log(f"生成 {json_path} ({len(alive)} 个节点)")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        die(f"程序异常: {type(exc).__name__}: {exc}")
