#!/usr/bin/env bash
# S4-基建 · 前端三项门禁一条命令跑完（task-28）
#
# 为什么要有这个脚本：`tsc --noEmit` / `test` / `build` 三个命令散落在 package.json、
# scripts/ci.py 与 workflow 里，人手跑容易只跑一项就宣布「前端没问题」。这里把三项串成
# 一条命令，**任一项失败也不提前退出**（要一次看到全貌），最后统一打印 SUMMARY 行。
#
# 用法：
#   bash scripts/check_frontend.sh              # tsc + test + build
#   bash scripts/check_frontend.sh --no-build   # 本地快跑，跳过 build（build 约 8-15s）
#
# 输出：全过程重定向到 .data/frontend-check-<timestamp>.log，控制台只回显每步 PASS/FAIL
# 与失败时的最后 20 行；SUMMARY 同时写进日志，便于 CI 归档后 grep。
#
# 退出码：0 = 全绿（含 --no-build 的 SKIP）；1 = 任一步 FAIL；2 = 参数/环境错误。
#
# 说明：build 步骤直接调 `pnpm --dir frontend build`（= tsc --noEmit && vite build），
# 因此 tsc 会被跑第二遍。这是故意的——不绕过项目自己定义的门禁命令，多花的十几秒
# 换来「脚本验的 = 人跑的 = CI 跑的」同一口径。

set -u

cd "$(dirname "$0")/.." || {
  echo "无法进入仓库根目录" >&2
  exit 2
}

run_build=1
for arg in "$@"; do
  case "$arg" in
    --no-build)
      run_build=0
      ;;
    -h | --help)
      echo "用法: bash scripts/check_frontend.sh [--no-build]"
      echo "  tsc   = pnpm --dir frontend exec tsc --noEmit"
      echo "  test  = pnpm --dir frontend test"
      echo "  build = pnpm --dir frontend build（--no-build 跳过）"
      exit 0
      ;;
    *)
      echo "未知参数: $arg（支持 --no-build / --help）" >&2
      exit 2
      ;;
  esac
done

for cmd in node pnpm; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "缺少命令: $cmd —— 前端门禁需要 node + pnpm（见 AGENTS §3）" >&2
    exit 2
  fi
done

stamp="$(date +%Y%m%dT%H%M%S)"
log_dir=".data"
log_file="$log_dir/frontend-check-$stamp.log"
mkdir -p "$log_dir" || {
  echo "无法创建日志目录: $log_dir" >&2
  exit 2
}

{
  echo "# 前端三项门禁  $(date '+%Y-%m-%d %H:%M:%S %z')"
  echo "# 仓库根: $(pwd)"
  echo "# node: $(node --version) / pnpm: $(pnpm --version)"
  echo "# 参数: $*"
} >"$log_file"

declare -A status=()
declare -A cost=()
summary_keys=()
failed=0

# 跑一步：全量输出进日志，控制台只给一行结论；失败时补最后 20 行。
run_step() {
  local name="$1"
  shift
  local started=$SECONDS

  printf '\n===== %s =====\n$ %s\n' "$name" "$*" >>"$log_file"
  if "$@" >>"$log_file" 2>&1; then
    status[$name]=PASS
  else
    status[$name]=FAIL
    failed=1
  fi
  cost[$name]=$((SECONDS - started))
  summary_keys+=("$name")

  printf '%-4s %-5s (%ss)\n' "${status[$name]}" "$name" "${cost[$name]}"
  if [ "${status[$name]}" = FAIL ]; then
    printf '  ---- %s 失败，最后 20 行（完整日志: %s）----\n' "$name" "$log_file"
    tail -n 20 "$log_file" | sed 's/^/  | /'
  fi
}

run_step tsc pnpm --dir frontend exec tsc --noEmit
run_step test pnpm --dir frontend test

if [ "$run_build" -eq 1 ]; then
  run_step build pnpm --dir frontend build
else
  # 跳过 ≠ 通过：SUMMARY 里标 SKIP，不假装绿（门禁「绿着跑 0 项」比没有门禁更坏）。
  status[build]=SKIP
  cost[build]=0
  summary_keys+=("build")
  printf '%-4s %-5s (--no-build)\n' "SKIP" "build"
fi

summary="SUMMARY:"
for key in "${summary_keys[@]}"; do
  printf -v piece ' %s=%s' "$key" "${status[$key]}"
  summary+=$piece
done
echo "$summary" | tee -a "$log_file"
echo "LOG: $log_file"

if [ "$failed" -ne 0 ]; then
  exit 1
fi
exit 0
