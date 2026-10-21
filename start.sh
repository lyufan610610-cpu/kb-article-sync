#!/usr/bin/env bash
# 启动 —— 等同 ./tmux.sh start
# 服务跑在 tmux 会话里，ssh 断开、终端关掉都不受影响，且不需要 sudo。
exec "$(cd "$(dirname "$0")" && pwd)/tmux.sh" start
