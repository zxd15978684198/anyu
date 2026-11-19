#!/usr/bin/env bash
# ============================================================================
# Let's Encrypt 续期后的 reload 钩子（公网部署用）
#
# 装到 /etc/letsencrypt/renewal-hooks/deploy/reload-anyu.sh 并 chmod +x。
# certbot renew 成功后自动执行；也可以手工跑一次验证。
#
# 为什么需要：nginx 与 coturn 都在容器里，证书文件是**只读挂载**。
# 续期只换了宿主机上的文件，容器内的进程不会自己重读 —— 不 reload 的话，
# 到期那一刻 HTTPS 与 TURNS 会同时失效，而 certbot 那边显示"续期成功"。
# ============================================================================
set -euo pipefail

COMPOSE_DIR="${COMPOSE_DIR:-/opt/anyu}"
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.prod.yml)

cd "$COMPOSE_DIR"

# nginx 支持平滑 reload：不断长连接（语音通话不会被掐断）。
docker compose "${COMPOSE_FILES[@]}" exec -T frontend nginx -s reload

# coturn 没有 reload 语义，只能重启；重启会断掉正在进行的 TURN 中继，
# 所以放在 nginx 之后，并允许它失败而不影响整体退出码（下次续期再修正）。
docker compose "${COMPOSE_FILES[@]}" restart coturn || echo "warn: coturn restart failed"

echo "cert hook done: $(date -Is)"
