#!/usr/bin/env bash
# 准备站点目录: 创建 site/ 并放入监控页。
# 数据文件 (ovpn.json / ovpn.yaml) 每次都由 ovpn.py 完整重新生成;
# ovpn.py 失败时工作流会直接停止、不部署, 所以不需要再从线上取回旧数据。
set -eu

mkdir -p site
cp web/index.html site/index.html
echo "已放入监控页: site/index.html"
