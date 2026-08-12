#!/bin/bash
# =============================================================================
# run_task.sh —— 下载任务包装脚本
# 负责并发槽位等待（最多同时下载 M3U8_JOB_DOWNLOAD_NUM 个任务）并启动 N_m3u8DL-RE。
# 用法: run_task.sh <task_dir> <url> <save_dir> <tmp_dir> <threads> <name> <max_jobs>
# 状态文件: <task_dir>/task.state  (waiting=排队中, downloading=下载中)
# =============================================================================
set -u

task_dir="$1"; url="$2"; save_dir="$3"; tmp_dir="$4"; threads="$5"; name="$6"; max_jobs="$7"
tasks_base="$(dirname "$task_dir")"
state_file="$task_dir/task.state"
log_file="$task_dir/log.txt"
n18_bin="${TRIM_APPDEST:-/var/apps/fn-daen-m3u8/target}/bin/N_m3u8DL-RE"
lock_dir="$tasks_base/.sched_lock"
[ "$max_jobs" -lt 1 ] 2>/dev/null && max_jobs=1

dl_pid=""
cleanup() {
    if [ -n "$dl_pid" ] && kill -0 "$dl_pid" 2>/dev/null; then
        kill -TERM "$dl_pid" 2>/dev/null
        sleep 1
        kill -KILL "$dl_pid" 2>/dev/null
    fi
    rm -f "$state_file"
    exit 0
}
trap 'cleanup' TERM INT

# ---- 等待并发槽位（原子锁 + 锁老化，防死锁） ----
echo "waiting" > "$state_file"
while :; do
    if mkdir "$lock_dir" 2>/dev/null; then
        # 锁内统计 downloading 数量并原子占位
        d=0
        for sf in "$tasks_base"/*/task.state; do
            [ -f "$sf" ] || continue
            [ "$(head -n 1 "$sf" 2>/dev/null)" = "downloading" ] && d=$((d + 1))
        done
        if [ "$d" -lt "$max_jobs" ]; then
            echo "downloading" > "$state_file"
            rmdir "$lock_dir" 2>/dev/null
            break
        fi
        rmdir "$lock_dir" 2>/dev/null
    else
        # 锁被占用：检查是否残留（超过 15 秒强制清理）
        if [ -d "$lock_dir" ]; then
            age=$(stat -c %Y "$lock_dir" 2>/dev/null || echo 0)
            now=$(date +%s)
            if [ "$age" -gt 0 ] && [ $((now - age)) -gt 15 ]; then
                rmdir "$lock_dir" 2>/dev/null
            fi
        fi
    fi
    sleep 1
done

# ---- 启动 N_m3u8DL-RE ----
if [ -n "$name" ]; then
    "$n18_bin" "$url" --save-name "$name" --save-dir "$save_dir" --tmp-dir "$tmp_dir" \
        --thread-count "$threads" --no-ansi-color --no-log --disable-update-check \
        --ui-language zh-CN --log-level INFO --del-after-done >> "$log_file" 2>&1 &
else
    "$n18_bin" "$url" --save-dir "$save_dir" --tmp-dir "$tmp_dir" \
        --thread-count "$threads" --no-ansi-color --no-log --disable-update-check \
        --ui-language zh-CN --log-level INFO --del-after-done >> "$log_file" 2>&1 &
fi
dl_pid=$!
wait "$dl_pid"
rm -f "$state_file"
exit 0
