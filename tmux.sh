#!/usr/bin/env bash
# ============================================================
# kb-sync 的 tmux 管理脚本
#
# 为什么用 tmux：服务跑在一个独立会话里，ssh 断开、终端关掉都不影响它。
# 全程不需要 sudo，也不用改系统配置（没有 sudo 权限也能用）。
#
#   ./tmux.sh start     启动（已经在跑就什么都不做）
#   ./tmux.sh stop      停止
#   ./tmux.sh restart   重启
#   ./tmux.sh status    看状态
#   ./tmux.sh logs      实时看日志（Ctrl+C 只是不看日志了，服务不停）
#   ./tmux.sh attach    进到会话里看输出（退出按 Ctrl+B，松开再按 D，服务不停）
#
# start.sh / stop.sh 就是 start / stop 的快捷方式，效果完全一样。
# ============================================================
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
SESSION="kb-sync"
LOG="$DIR/logs/app.log"

if [ -f "$DIR/env.sh" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$DIR/env.sh"
    set +a
fi
PORT="${PORT:-8090}"

# 服务进程的 pid（按命令行找，比 pid 文件可靠，不依赖谁启动的）
svc_pid() {
    pgrep -f "waitress --listen=0.0.0.0:${PORT} post:app" 2>/dev/null | head -n1
}

healthy() {
    curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1
}

do_start() {
    command -v tmux >/dev/null 2>&1 || { echo "[kb-sync] 这台机器上没有 tmux，用不了本脚本"; exit 1; }
    mkdir -p "$DIR/logs"

    if tmux has-session -t "$SESSION" 2>/dev/null; then
        if healthy; then
            echo "[kb-sync] 已经在跑 pid=$(svc_pid)  端口 ${PORT}"
            return 0
        fi
        echo "[kb-sync] 会话 $SESSION 还在但服务没响应，杀掉重建"
        tmux kill-session -t "$SESSION" 2>/dev/null
        sleep 1
    fi

    # 会话里跑 run.sh，输出同时写进日志文件。
    # 会话是独立的，所以 ssh 断开、终端关掉都不会把服务带走。
    tmux new-session -d -s "$SESSION" -c "$DIR" "./run.sh 2>&1 | tee -a \"$LOG\""

    for _ in $(seq 1 30); do
        sleep 0.5
        if healthy; then
            echo "[kb-sync] 已启动 pid=$(svc_pid)  端口 ${PORT}"
            curl -s "http://127.0.0.1:${PORT}/healthz"; echo
            echo "[kb-sync] 现在断开 ssh 也不影响。看日志：./tmux.sh logs"
            return 0
        fi
    done

    echo "[kb-sync] 启动失败，最近日志："
    tail -n 20 "$LOG" 2>/dev/null
    exit 1
}

do_stop() {
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        tmux kill-session -t "$SESSION" 2>/dev/null
        echo "[kb-sync] 已关闭 tmux 会话 $SESSION"
    else
        echo "[kb-sync] 没有 tmux 会话 $SESSION"
    fi

    # 兜底：会话没了但进程还占着端口（比如以前用 start.sh 起的），补一刀
    sleep 1
    pid="$(svc_pid)"
    if [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null
        sleep 1
        pid="$(svc_pid)"
        [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null
    fi
    rm -f "$DIR/logs/app.pid"

    if healthy; then
        echo "[kb-sync] 端口 ${PORT} 还有响应，看看谁占着：ss -lptn 'sport = :${PORT}'"
        return 1
    fi
    echo "[kb-sync] 已停止"
}

do_status() {
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "[kb-sync] tmux 会话 $SESSION：在"
    else
        echo "[kb-sync] tmux 会话 $SESSION：不在"
    fi
    pid="$(svc_pid)"
    echo "[kb-sync] 进程 pid：${pid:-无}"
    echo -n "[kb-sync] /healthz："
    if healthy; then
        curl -s "http://127.0.0.1:${PORT}/healthz"; echo
    else
        echo "没响应"
    fi
}

case "${1:-start}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; do_start ;;
    status)  do_status ;;
    logs)    tail -n 50 -f "$LOG" ;;
    attach)  tmux attach -t "$SESSION" ;;
    *)       echo "用法：$0 {start|stop|restart|status|logs|attach}"; exit 2 ;;
esac
