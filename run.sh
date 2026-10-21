#!/usr/bin/env bash
# 前台启动（Ctrl+C 停止）
cd "$(dirname "$0")" || exit 1
set -a
. ./env.sh
set +a
echo "[kb-sync] http://0.0.0.0:${PORT}  ->  dify ${DIFY_BASE}"
exec ./venv/bin/python -m waitress --listen=0.0.0.0:${PORT} post:app
