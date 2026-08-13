#!/bin/bash
# =============================================================================
# fn-daen-m3u8 CGI 入口
#
# 功能：
#   1. API：创建下载任务、查询任务状态、查看日志、停止任务
#   2. 静态文件服务（app/ui/www 下的资源）
#
# 下载由 N_m3u8DL-RE 完成，任务在后台独立运行，CGI 请求快速返回，
# 前端通过轮询 status 接口获取进度。
# =============================================================================

APP_NAME="fn-daen-m3u8"
VAR_DIR="${TRIM_PKGVAR:-/var/apps/${APP_NAME}/var}"

# CGI 进程不注入 TRIM_* 环境变量；从生命周期脚本（cmd/main、config_callback 等）
# 持久化的 app_env.conf 读取。文件存在时以它为准（值更新、更权威）。
ENV_CONF="${VAR_DIR}/app_env.conf"
if [ -r "$ENV_CONF" ]; then
    . "$ENV_CONF"
fi

APP_DEST="${TRIM_APPDEST:-/var/apps/${APP_NAME}/target}"
VAR_DIR="${TRIM_PKGVAR:-/var/apps/${APP_NAME}/var}"
BASE_PATH="${APP_DEST}/ui/www"
N18_BIN="${APP_DEST}/bin/N_m3u8DL-RE"
TASKS_DIR="${VAR_DIR}/tasks"

# ---- 下载输出目录：优先使用 data-share 的 <app>/data 共享目录（用户在文件管理器可见）----
OUT_ROOT=""
if [ -n "${TRIM_DATA_SHARE_PATHS:-}" ]; then
  IFS=':' read -r -a SHARE_ARR <<< "$TRIM_DATA_SHARE_PATHS"
  for d in "${SHARE_ARR[@]}"; do
    case "$d" in
      */data) OUT_ROOT="$d"; break ;;
    esac
  done
fi
if [ -z "$OUT_ROOT" ] && [ -d "/var/apps/${APP_NAME}/share/${APP_NAME}/data" ]; then
  OUT_ROOT="/var/apps/${APP_NAME}/share/${APP_NAME}/data"
fi
if [ -z "$OUT_ROOT" ]; then
  OUT_ROOT="${VAR_DIR}/downloads"
fi

# ---- 用户授权目录：应用设置中授权（TRIM_DATA_ACCESSIBLE_PATHS，: 分隔）----
ACCESSIBLE_DIRS=()
if [ -n "${TRIM_DATA_ACCESSIBLE_PATHS:-}" ]; then
  IFS=':' read -r -a ACCESSIBLE_DIRS <<< "$TRIM_DATA_ACCESSIBLE_PATHS"
fi
# 未授权任何目录时，可用目录降级为 data-share 共享目录
if [ "${#ACCESSIBLE_DIRS[@]}" -eq 0 ]; then
  ACCESSIBLE_DIRS=("$OUT_ROOT")
fi

# 判断目录是否在授权列表内（拒绝任意路径）
dir_allowed() {
  local d="$1" x
  for x in "${ACCESSIBLE_DIRS[@]}"; do
    if [ "$d" = "$x" ]; then return 0; fi
  done
  return 1
}

# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------

# 从 QUERY_STRING 取参数
get_query_param() {
  local key="$1" qs="$2" pair="" v=""
  local IFS='&'
  for pair in $qs; do
    case "$pair" in
      "${key}="*) v="${pair#*=}" ;;
    esac
  done
  printf '%s' "$v"
}

# 读取 POST body
read_post_body() {
  if [ "${REQUEST_METHOD:-GET}" = "POST" ]; then
    dd bs=1 count="${CONTENT_LENGTH:-0}" 2>/dev/null
  fi
}

# URL 解码（%XX -> 字符）
urldecode() {
  local s="$1" out="" ch=""
  s="${s//+/ }"
  while [ -n "$s" ]; do
    case "$s" in
      %??*)
        ch=$(printf "\\x${s:1:2}" 2>/dev/null)
        s="${s:3}"
        ;;
      *)
        ch="${s:0:1}"
        s="${s:1}"
        ;;
    esac
    out="${out}${ch}"
  done
  printf '%s' "$out"
}

# URL 合法性校验：仅允许 http/https，且不含空白/引号/反斜杠/美元符/反引号
validate_url() {
  local u="$1"
  case "$u" in
    http://*|https://*) ;;
    *) return 1 ;;
  esac
  case "$u" in
    *' '*|*$'\t'*|*$'\n'*|*'"'*|*'\'*|*'$'*|*'`'*) return 1 ;;
  esac
  return 0
}

# 文件夹名校验：允许空（不创建子文件夹）；非空时限制字符与长度
validate_folder_name() {
  local f="$1"
  if [ -z "$f" ]; then return 0; fi
  [ "${#f}" -gt 100 ] && return 1
  case "$f" in
    .|..) return 1 ;;
    */*|*'\'*|*'..'*|*'"'*|*'$'*|*'`'*|*'*'*|*'?'*|*':'*|*'<'*|*'>'*|*'|'*|*$'\n'*|*$'\t'*) return 1 ;;
  esac
  return 0
}

# 保存文件名校验（用于 "名称$url" 的 $ 前部分）；允许空（使用自动命名）
validate_name() {
  local n="$1"
  if [ -z "$n" ]; then return 0; fi
  [ "${#n}" -gt 200 ] && return 1
  case "$n" in
    .|..) return 1 ;;
    */*|*'\'*|*'..'*|*'"'*|*'$'*|*'`'*|*'*'*|*'?'*|*':'*|*'<'*|*'>'*|*'|'*|*$'\n'*|*$'\t'*) return 1 ;;
  esac
  return 0
}

# 任务 ID 格式校验（仅数字/小写十六进制/下划线，防路径穿越）
valid_task_id() {
  local id="$1"
  case "$id" in
    ''|*[!0-9a-f_]*) return 1 ;;
    *_[0-9a-f]*[0-9a-f_]) return 0 ;;
    *) return 1 ;;
  esac
}

# JSON 字符串转义
json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\r'/\\r}"
  s="${s//$'\t'/\\t}"
  printf '%s' "$s"
}

# 任务实际保存目录（从 task.json 读取；老格式缺省 OUT_ROOT/<id>）
task_out_dir() {
  local id="$1" d
  d=$(grep -o '"out_dir":"[^"]*"' "${TASKS_DIR}/${id}/task.json" 2>/dev/null | head -n1 | sed 's/^"out_dir":"//; s/"$//')
  if [ -n "$d" ]; then
    printf '%s' "$d"
  else
    printf '%s' "${OUT_ROOT}/${id}"
  fi
}

# 任务进度：从日志实时提取最新 "X/Y ZZ.ZZ%"（日志被删除/清空后无进度，属正常）
task_progress() {
  local id="$1" log="${TASKS_DIR}/${id}/log.txt" line pct seg
  if [ -f "$log" ]; then
    line=$(tr '\r' '\n' < "$log" | grep -oE '[0-9]+/[0-9]+ [0-9.]+%' | tail -n1)
    if [ -n "$line" ]; then
      seg="${line%% *}"
      pct="${line##* }"
      pct="${pct%\%}"
      printf '{"pct":%s,"seg":"%s"}' "$pct" "$seg"
      return
    fi
  fi
  printf 'null'
}

# 任务状态：waiting | running | done | failed
task_status() {
  local id="$1" pid="" has_out=0 state=""
  if [ -f "${TASKS_DIR}/${id}/task.pid" ]; then
    pid=$(head -n 1 "${TASKS_DIR}/${id}/task.pid" | tr -d '[:space:]')
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      # wrapper 存活：看排队状态
      if [ -f "${TASKS_DIR}/${id}/task.state" ]; then
        state=$(head -n 1 "${TASKS_DIR}/${id}/task.state" | tr -d '[:space:]')
        if [ "$state" = "waiting" ]; then
          printf 'waiting'
          return
        fi
      fi
      printf 'running'
      return
    fi
  fi
  # 进程已退出：看保存目录是否产生媒体文件
  local od
  od=$(task_out_dir "$id")
  if [ -d "$od" ]; then
    if find "$od" -maxdepth 1 -type f \( -name '*.mp4' -o -name '*.mkv' -o -name '*.ts' -o -name '*.m4a' -o -name '*.srt' \) 2>/dev/null | grep -q .; then
      has_out=1
    fi
  fi
  if [ "$has_out" -eq 1 ]; then printf 'done'; else printf 'failed'; fi
}

# 输出文件列表（basename）
output_files() {
  local id="$1" od
  od=$(task_out_dir "$id")
  if [ -d "$od" ]; then
    find "$od" -maxdepth 1 -type f \( -name '*.mp4' -o -name '*.mkv' -o -name '*.ts' -o -name '*.m4a' -o -name '*.srt' \) -printf '%f\n' 2>/dev/null
  fi
}

# 生成单个任务的 JSON 片段
task_json() {
  local id="$1" url="" name="" threads="" started="" st="" od="" pr=""
  url=$(grep -o '"url":"[^"]*"' "${TASKS_DIR}/${id}/task.json" | head -n1 | sed 's/^"url":"//; s/"$//')
  name=$(grep -o '"name":"[^"]*"' "${TASKS_DIR}/${id}/task.json" | head -n1 | sed 's/^"name":"//; s/"$//')
  threads=$(grep -o '"threads":[0-9]*' "${TASKS_DIR}/${id}/task.json" | head -n1 | sed 's/"threads"://')
  started=$(grep -o '"started_at":"[^"]*"' "${TASKS_DIR}/${id}/task.json" | head -n1 | sed 's/^"started_at":"//; s/"$//')
  od=$(task_out_dir "$id")
  st=$(task_status "$id")
  pr=$(task_progress "$id")
  printf '{"id":"%s","url":"%s","name":"%s","threads":%s,"status":"%s","progress":%s,"started_at":"%s","out_dir":"%s","output_files":[' \
    "$id" "$(json_escape "$url")" "$(json_escape "$name")" "${threads:-5}" "$st" "$pr" "$(json_escape "$started")" "$(json_escape "$od")"
  local first=1 f
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if [ "$first" -eq 1 ]; then first=0; else printf ','; fi
    printf '"%s"' "$(json_escape "$f")"
  done < <(output_files "$id")
  printf ']}'
}

# ----------------------------------------------------------------------------
# API 处理
# ----------------------------------------------------------------------------

ACTION=$(get_query_param action "$QUERY_STRING")

case "$ACTION" in
  download)
    if [ "${REQUEST_METHOD:-GET}" != "POST" ]; then
      printf 'Status: 405 Method Not Allowed\nContent-Type: application/json\n\n{"error":"POST required"}'
      exit 0
    fi
    BODY=$(read_post_body)
    mkdir -p "$TASKS_DIR"

    # 统一解析表单字段：threads / dir / folder / url（可多个）
    threads=""
    dir=""
    folder=""
    urls=()
    names=()
    _SAVED_IFS="$IFS"
    IFS='&'
    for pair in $BODY; do
      case "$pair" in
        "threads="*) threads="${pair#threads=}" ;;
        "dir="*) dir=$(urldecode "${pair#dir=}") ;;
        "folder="*) folder=$(urldecode "${pair#folder=}") ;;
        "url="*)
          v=$(urldecode "${pair#url=}")
          # 支持 "名称$url" 或 "名称$url$ffm3u8"（$ 后的无用后缀丢弃）；$ 前为保存文件名
          name=""
          case "$v" in
            *'$'*)
              name="${v%%\$*}"
              v="${v#*\$}"
              v="${v%%\$*}"
              ;;
          esac
          if validate_url "$v" && validate_name "$name"; then
            urls+=("$v")
            names+=("$name")
          fi
          ;;
      esac
    done
    IFS="$_SAVED_IFS"

    # 线程数：默认取 wizard 配置 M3U8_JOB_NUM，兜底 5
    [ -z "$threads" ] && threads="${M3U8_JOB_NUM:-5}"
    case "$threads" in
      ''|*[!0-9]*) threads=5 ;;
    esac
    [ "$threads" -lt 1 ] && threads=1
    [ "$threads" -gt 64 ] && threads=64

    # 目录必须在授权列表内（拒绝任意路径）
    if [ -n "$dir" ] && ! dir_allowed "$dir"; then
      printf 'Status: 400 Bad Request\nContent-Type: application/json\n\n{"error":"dir not allowed"}'
      exit 0
    fi
    # 文件夹名：允许空（不创建子文件夹）
    if ! validate_folder_name "$folder"; then
      printf 'Status: 400 Bad Request\nContent-Type: application/json\n\n{"error":"invalid folder name"}'
      exit 0
    fi

    if [ "${#urls[@]}" -eq 0 ]; then
      printf 'Status: 400 Bad Request\nContent-Type: application/json\n\n{"error":"no valid urls"}'
      exit 0
    fi

    # 计算实际保存目录：授权目录(或默认共享目录) + 可选子文件夹
    BASE_DIR="$OUT_ROOT"
    if [ -n "$dir" ]; then BASE_DIR="$dir"; fi
    if [ -n "$folder" ]; then
      SAVE_DIR="${BASE_DIR}/${folder}"
      # 拒绝符号链接目录（防止绕过授权目录）
      if [ -L "$SAVE_DIR" ] || ! mkdir -p "$SAVE_DIR" 2>/dev/null; then
        printf 'Status: 500 Internal Server Error\nContent-Type: application/json\n\n{"error":"cannot create folder"}'
        exit 0
      fi
    else
      SAVE_DIR="$BASE_DIR"
    fi

    # 并发下载数：wizard 配置 M3U8_JOB_DOWNLOAD_NUM，兜底 3
    max_jobs=""
    case "${M3U8_JOB_DOWNLOAD_NUM:-}" in
      ''|*[!0-9]*) max_jobs=3 ;;
      *) max_jobs="${M3U8_JOB_DOWNLOAD_NUM}" ;;
    esac
    [ "$max_jobs" -lt 1 ] && max_jobs=1

    # 逐个创建任务：由 run_task.sh 包装启动（等待并发槽位）
    printf 'Content-Type: application/json\n\n{"ok":true,"tasks":['
    first=1
    for i in "${!urls[@]}"; do
      u="${urls[$i]}"
      nm="${names[$i]}"
      id="$(date +%s)_$(printf '%04x' $((RANDOM % 65536)))"
      mkdir -p "$SAVE_DIR" "${TASKS_DIR}/${id}/tmp"
      cat > "${TASKS_DIR}/${id}/task.json" <<EOF
{"id":"$id","url":"$(json_escape "$u")","name":"$(json_escape "$nm")","threads":$threads,"dir":"$(json_escape "$dir")","folder":"$(json_escape "$folder")","out_dir":"$(json_escape "$SAVE_DIR")","started_at":"$(date '+%Y-%m-%d %H:%M:%S')"}
EOF
      LOG_FILE="${TASKS_DIR}/${id}/log.txt"
      nohup bash "${APP_DEST}/bin/run_task.sh" \
        "${TASKS_DIR}/${id}" \
        "$u" \
        "$SAVE_DIR" \
        "${TASKS_DIR}/${id}/tmp" \
        "$threads" \
        "$nm" \
        "$max_jobs" \
        >> "$LOG_FILE" 2>&1 &
      echo "$!" > "${TASKS_DIR}/${id}/task.pid"
      if [ "$first" -eq 1 ]; then first=0; else printf ','; fi
      printf '{"id":"%s","url":"%s","name":"%s"}' "$id" "$(json_escape "$u")" "$(json_escape "$nm")"
    done
    printf ']}'
    ;;

  dirs)
    dt=""
    case "${M3U8_JOB_NUM:-}" in
      ''|*[!0-9]*) ;;
      *) dt="${M3U8_JOB_NUM}" ;;
    esac
    dm=""
    case "${M3U8_JOB_DOWNLOAD_NUM:-}" in
      ''|*[!0-9]*) ;;
      *) dm="${M3U8_JOB_DOWNLOAD_NUM}" ;;
    esac
    dr=""
    case "${M3U8_REFRESH_SECOND:-}" in
      ''|*[!0-9]*) ;;
      *) dr="${M3U8_REFRESH_SECOND}" ;;
    esac
    printf 'Content-Type: application/json\n\n{"dirs":['
    first=1
    for d in "${ACCESSIBLE_DIRS[@]}"; do
      [ -n "$d" ] || continue
      if [ "$first" -eq 1 ]; then first=0; else printf ','; fi
      printf '"%s"' "$(json_escape "$d")"
    done
    printf '],"default_threads":"%s","max_jobs":"%s","refresh_seconds":"%s"}' "$dt" "$dm" "$dr"
    ;;

  delete_log)
    # 删除整个任务目录（任务信息、日志、临时文件），同时停止仍在运行的下载进程；
    # 已下载的视频文件位于保存目录，不受影响。
    id=$(get_query_param id "$QUERY_STRING")
    if valid_task_id "$id" && [ -d "${TASKS_DIR}/${id}" ]; then
      if [ -f "${TASKS_DIR}/${id}/task.pid" ]; then
        pid=$(head -n 1 "${TASKS_DIR}/${id}/task.pid" | tr -d '[:space:]')
        if [ -n "$pid" ]; then
          kill -TERM "$pid" 2>/dev/null
          kill -KILL "$pid" 2>/dev/null
        fi
      fi
      rm -rf "${TASKS_DIR}/${id}"
      printf 'Content-Type: application/json\n\n{"ok":true,"removed":"%s"}' "$id"
    else
      printf 'Content-Type: application/json\n\n{"ok":true,"removed":"%s","note":"no task"}' "$id"
    fi
    ;;

  clear_tasks)
    # 清空任务：无论下载状态，删除所有任务；正在下载的直接终止进程
    removed=0
    if [ -d "$TASKS_DIR" ]; then
      for d in "$TASKS_DIR"/*/; do
        [ -d "$d" ] || continue
        id=$(basename "$d")
        [ -f "${d}task.json" ] || continue
        # 停止任务进程（wrapper 收到 TERM 会连带终止 N_m3u8DL-RE）
        if [ -f "${d}task.pid" ]; then
          pid=$(head -n 1 "${d}task.pid" | tr -d '[:space:]')
          if [ -n "$pid" ]; then
            kill -TERM "$pid" 2>/dev/null
            sleep 1
            kill -KILL "$pid" 2>/dev/null
          fi
        fi
        rm -rf "$d" && removed=$((removed + 1))
      done
      # 清理可能的调度锁残留
      rm -rf "${TASKS_DIR}/.sched_lock"
    fi
    printf 'Content-Type: application/json\n\n{"ok":true,"removed":%s}' "$removed"
    ;;

  status)
    # 返回全部任务，最新创建的任务排最前
    ids=()
    if [ -d "$TASKS_DIR" ]; then
      for d in "$TASKS_DIR"/*/; do
        [ -d "$d" ] || continue
        id=$(basename "$d")
        [ -f "${d}task.json" ] || continue
        ids+=("$id")
      done
    fi
    # 最新在前：任务 id 以创建时间戳开头，字典序倒序即为时间倒序
    mapfile -t ids < <(printf '%s\n' "${ids[@]}" | sort -r)
    total=${#ids[@]}
    printf 'Content-Type: application/json\n\n{"total":%s,"tasks":[' "$total"
    first=1
    for id in "${ids[@]}"; do
      if [ "$first" -eq 1 ]; then first=0; else printf ','; fi
      task_json "$id"
    done
    printf ']}'
    ;;

  log)
    id=$(get_query_param id "$QUERY_STRING")
    LOG_TEXT=""
    if valid_task_id "$id" && [ -f "${TASKS_DIR}/${id}/log.txt" ]; then
      # 完整输出；\r（进度条刷新分隔符）转 \n 保持可读；去除 ANSI 颜色
      LOG_TEXT=$(cat "${TASKS_DIR}/${id}/log.txt" | tr -d '\000' | tr '\r' '\n' | sed 's/\x1b\[[0-9;]*m//g')
    fi
    printf 'Content-Type: application/json\n\n{"id":"%s","log":"%s"}' "$(json_escape "$id")" "$(json_escape "$LOG_TEXT")"
    ;;

  stop)
    id=$(get_query_param id "$QUERY_STRING")
    if valid_task_id "$id" && [ -f "${TASKS_DIR}/${id}/task.pid" ]; then
      pid=$(head -n 1 "${TASKS_DIR}/${id}/task.pid" | tr -d '[:space:]')
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null
        cnt=0
        while kill -0 "$pid" 2>/dev/null && [ "$cnt" -lt 10 ]; do
          sleep 1
          cnt=$((cnt + 1))
        done
        kill -KILL "$pid" 2>/dev/null
        printf 'Content-Type: application/json\n\n{"ok":true,"stopped":"%s"}' "$id"
      else
        printf 'Content-Type: application/json\n\n{"ok":true,"stopped":"%s","note":"not running"}' "$id"
      fi
    else
      printf 'Content-Type: application/json\n\n{"error":"invalid id"}'
    fi
    ;;

  *) # ---- 静态文件服务（原有逻辑） ----
    URI_NO_QUERY="${REQUEST_URI%%\?*}"
    REL_PATH="/"
    case "$URI_NO_QUERY" in
      *index.cgi*)
        REL_PATH="${URI_NO_QUERY#*index.cgi}"
        ;;
    esac

    if [ -z "$REL_PATH" ] || [ "$REL_PATH" = "/" ]; then
      REL_PATH="/index.html"
    fi

    TARGET_FILE="${BASE_PATH}${REL_PATH}"

    if echo "$TARGET_FILE" | grep -q '\.\.'; then
      echo "Status: 400 Bad Request"
      echo "Content-Type: text/plain; charset=utf-8"
      echo ""
      echo "Bad Request"
      exit 0
    fi

    if [ ! -f "$TARGET_FILE" ]; then
      echo "Status: 404 Not Found"
      echo "Content-Type: text/plain; charset=utf-8"
      echo ""
      echo "404 Not Found"
      exit 0
    fi

    case "${TARGET_FILE##*.}" in
      html|htm) mime="text/html; charset=utf-8" ;;
      css) mime="text/css; charset=utf-8" ;;
      js) mime="application/javascript; charset=utf-8" ;;
      png) mime="image/png" ;;
      jpg|jpeg) mime="image/jpeg" ;;
      svg) mime="image/svg+xml" ;;
      *) mime="application/octet-stream" ;;
    esac

    echo "Content-Type: $mime"
    echo ""
    cat "$TARGET_FILE"
    ;;
esac
