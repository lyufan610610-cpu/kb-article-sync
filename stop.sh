#!/usr/bin/env bash
# 停止 —— 等同 ./tmux.sh stop
exec "$(cd "$(dirname "$0")" && pwd)/tmux.sh" stop
