#!/usr/bin/env bash
# 工作流共用: 把已发布 Pages 上的数据文件取回 site/, 再放入网页。
# 文件名规则: ovpn.json 由 ovpn.py 生成。
# 数据文件只在工作流运行时生成并发布, 不提交到仓库。
set -u

owner="${GITHUB_REPOSITORY_OWNER:-owner}"
repo="${GITHUB_REPOSITORY:-owner/repo}"
base="${PAGES_URL:-https://${owner,,}.github.io/${repo#*/}}"
base="${base%/}"

# pool.txt / pool.yaml / sstp.txt / sstp.yaml / ovpn.yaml 不在 Pages 上, 由私有 Worker 保存, 这里不恢复
# 恢复列表以 common.py 的 PUBLIC_FILES 为准 (单一来源)
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mapfile -t FILES < <(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from common import PUBLIC_FILES; print("\n".join(PUBLIC_FILES))' "$ROOT")

# 取回的内容必须像样: .json 要能解析, 其余不能为空。
# (Pages 出错时可能返回 HTTP 200 的 HTML 错误页, 不校验就会被原样重新部署)
valid() {   # $1=待检查的文件  $2=它最终的文件名 (按扩展名决定校验方式)
  case "$2" in
    *.json) python3 -c 'import json, sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$1" 2>/dev/null ;;
    *) [ -s "$1" ] ;;
  esac
}

mkdir -p site

for f in "${FILES[@]}"; do
  tmp="site/$f.tmp"
  if curl -fsS --retry 2 --max-time 30 -o "$tmp" "$base/$f" && valid "$tmp" "$f"; then
    mv "$tmp" "site/$f"
    echo "已恢复: $f"
  else
    rm -f "$tmp"
    echo "无旧文件或内容无效: $f"
  fi
done

cp web/index.html site/index.html

# 后续步骤可用 $SITE_URL (gate.py 用它生成"固定地址"注释)
[ -n "${GITHUB_ENV:-}" ] && echo "SITE_URL=$base" >> "$GITHUB_ENV"
exit 0
