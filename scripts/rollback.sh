#!/usr/bin/env bash
# =============================================================================
# 安语（Anyu）· 迭代 10 · W6 —— 代码 + 迁移回滚
# =============================================================================
# 目的：一条命令把「代码 + 数据库 schema + 容器」退回上一个 tag，并证明服务重新健康。
# 设计口径：设计记录（交付与观测） §4-W6、§6.1-5、§6.2-3；TD §8 迭代 10
#           「改一行 → 故意坏 → 回滚到上一 tag」；风险条「迁移回滚与代码回滚要分别演练」。
#
# 用法：
#   scripts/rollback.sh [--yes] [--dry-run] [--ref <tag|commit>] [--no-migrate]
#                       [--force-dirty] [--force-ref]
#
#   --yes          跳过交互确认（非交互环境 / CI 必须显式给）
#   --dry-run      只打印将要执行的命令，一条都不执行（零副作用；恒退出 0）
#   --ref <ref>    目标 tag 或 commit；缺省取最近一个 tag：git describe --tags --abbrev=0 HEAD
#   --no-migrate   只回代码，不动数据库 schema（「代码回滚」单练时用）
#   --force-dirty  工作树有未提交改动时仍然继续（**会丢改动**；必须同时给 --yes）
#   --force-ref    目标 ref 不在 HEAD 祖先链上 / 不是 commit 时仍然继续（危险，人工兜底）
#   -h, --help     打印本帮助
#
# 环境变量（**不读 .env**，全部走进程环境；.env 由 docker compose / Pydantic Settings 自己读）：
#   BACKEND_HEALTH_URL        健康检查地址，默认 http://127.0.0.1:${BACKEND_HOST_PORT:-8000}/health
#   BACKEND_HOST_PORT         后端宿主端口，默认 8000
#   ROLLBACK_HEALTH_RETRIES   探活次数，默认 30
#   ROLLBACK_HEALTH_INTERVAL  探活间隔（秒），默认 2
#   ROLLBACK_DB_SERVICES      迁移前要起的 compose 数据层服务，默认 "postgres redis"
#   ALEMBIC_CMD               alembic 调用前缀（如 "uv run --locked alembic"），默认自动探测
#
# 退出码：0 = 成功（dry-run 亦是 0）/ 1 = 前置检查或执行失败 / 2 = 用法错误
# =============================================================================
set -euo pipefail

# 只读的 git 调用（status / log / rev-parse）默认会顺手刷新 .git/index 的 stat 缓存。
# 关掉 optional locks 后，dry-run 与只读检查对仓库真正做到零写入。
export GIT_OPTIONAL_LOCKS=0

# -----------------------------------------------------------------------------
# 全局状态
# -----------------------------------------------------------------------------
DRY_RUN=0
ASSUME_YES=0
FORCE_DIRTY=0
FORCE_REF=0
DO_MIGRATE=1
REF_ARG=''

REPO_ROOT=''
ORIGINAL_SHA=''
ORIGINAL_SHORT=''
ORIGINAL_DESC=''
ORIGINAL_SUBJECT=''
TARGET_REF=''
TARGET_SHA=''
TARGET_SHORT=''
TARGET_SUBJECT=''

HEALTH_URL="${BACKEND_HEALTH_URL:-http://127.0.0.1:${BACKEND_HOST_PORT:-8000}/health}"
HEALTH_RETRIES="${ROLLBACK_HEALTH_RETRIES:-30}"
HEALTH_INTERVAL="${ROLLBACK_HEALTH_INTERVAL:-2}"
DB_SERVICES="${ROLLBACK_DB_SERVICES:-postgres redis}"

BLOCKERS=()
MIGRATION_VERDICT='（未执行；dry-run 只做静态预判）'
HEALTH_VERDICT='（未执行；dry-run 只打印命令）'
ALEMBIC_BIN=()

# -----------------------------------------------------------------------------
# 输出
# -----------------------------------------------------------------------------
hr() { printf '%s\n' '--------------------------------------------------------------------------'; }
step() { printf '\n[%s/8] %s\n' "$1" "$2"; }
info() { printf '    [INFO]  %s\n' "$*"; }
ok() { printf '    [OK]    %s\n' "$*"; }
warn() { printf '    [WARN]  %s\n' "$*"; }
bad() { printf '    [BLOCK] %s\n' "$*"; }
note() { printf '            %s\n' "$*"; }

blocker() {
  BLOCKERS+=("$1")
  bad "$1"
}

die() {
  printf '\n[FAIL] %s\n' "$*" >&2
  exit 1
}

# 把 argv 打印成「可复制粘贴的一行」。printf %q 保证带空格/引号的参数可回溯。
print_cmd() {
  local out='' a
  for a in "$@"; do
    out+="$(printf '%q' "$a") "
  done
  printf '    $ %s\n' "${out% }"
}

# 打印 + 执行；--dry-run 时只打印。
run() {
  print_cmd "$@"
  if (( DRY_RUN == 1 )); then
    return 0
  fi
  "$@"
}

# -----------------------------------------------------------------------------
# 用法
# -----------------------------------------------------------------------------
usage() {
  cat <<'EOF'
安语（Anyu）回滚脚本 · 代码 + 迁移

用法：
  scripts/rollback.sh [--yes] [--dry-run] [--ref <tag|commit>] [--no-migrate]
                      [--force-dirty] [--force-ref]

参数：
  --yes          跳过交互确认（非交互环境 / CI 必须显式给）
  --dry-run      只打印将要执行的命令，一条都不执行（零副作用；恒退出 0）
  --ref <ref>    目标 tag 或 commit；缺省取最近一个 tag
                 （git describe --tags --abbrev=0 HEAD）
  --no-migrate   只回代码，不动数据库 schema
  --force-dirty  工作树有未提交改动时仍然继续（会丢改动；必须同时给 --yes）
  --force-ref    目标 ref 不在 HEAD 祖先链上 / 不是 commit 时仍然继续
  -h, --help     打印本帮助

环境变量：BACKEND_HEALTH_URL、BACKEND_HOST_PORT、ROLLBACK_HEALTH_RETRIES、
          ROLLBACK_HEALTH_INTERVAL、ROLLBACK_DB_SERVICES、ALEMBIC_CMD

退出码：0 成功（dry-run 也是 0）/ 1 前置检查或执行失败 / 2 用法错误
详见 docs/rollback.md
EOF
}

die_usage() {
  printf '\n[用法错误] %s\n\n' "$*" >&2
  usage >&2
  exit 2
}

# -----------------------------------------------------------------------------
# 参数解析
# -----------------------------------------------------------------------------
parse_args() {
  while (($# > 0)); do
    case "$1" in
      --yes) ASSUME_YES=1 ;;
      --dry-run) DRY_RUN=1 ;;
      --no-migrate) DO_MIGRATE=0 ;;
      --force-dirty) FORCE_DIRTY=1 ;;
      --force-ref) FORCE_REF=1 ;;
      --ref)
        (($# >= 2)) || die_usage '--ref 需要一个参数（tag 或 commit）'
        REF_ARG="$2"
        shift
        ;;
      --ref=*)
        REF_ARG="${1#--ref=}"
        [[ -n $REF_ARG ]] || die_usage '--ref 需要一个参数（tag 或 commit）'
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        die_usage "不认识的参数：$1"
        ;;
    esac
    shift
  done

  if (( FORCE_DIRTY == 1 )) && (( ASSUME_YES == 0 )); then
    die_usage '--force-dirty 会丢弃未提交改动，必须同时给 --yes'
  fi
}

# -----------------------------------------------------------------------------
# 通用小工具
# -----------------------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

resolve_alembic() {
  if [[ -n ${ALEMBIC_CMD:-} ]]; then
    read -r -a ALEMBIC_BIN <<<"$ALEMBIC_CMD"
    return 0
  fi
  if have uv && [[ -f uv.lock ]]; then
    ALEMBIC_BIN=(uv run --locked alembic)
  elif have alembic; then
    ALEMBIC_BIN=(alembic)
  elif [[ -x .venv/bin/alembic ]]; then
    ALEMBIC_BIN=(./.venv/bin/alembic)
  elif [[ -f .venv/Scripts/alembic.exe ]]; then
    ALEMBIC_BIN=(./.venv/Scripts/alembic.exe)
  else
    ALEMBIC_BIN=()
    return 1
  fi
  return 0
}

# 从目录里取「最新迁移号」（文件名的前 4 位数字，如 0009）。
mig_number_from_dir() {
  local dir="$1" f base num max=''
  [[ -d $dir ]] || {
    printf ''
    return 0
  }
  for f in "$dir"/*.py; do
    [[ -e $f ]] || continue
    base="${f##*/}"
    [[ $base == [0-9][0-9][0-9][0-9]_*.py ]] || continue
    num="${base%%_*}"
    if [[ -z $max || $num > $max ]]; then
      max="$num"
    fi
  done
  printf '%s' "$max"
}

# 从某个 ref 的树里取「最新迁移号」。回滚前看一眼目标版本有哪些迁移。
mig_number_from_ref() {
  local ref="$1" p base num max=''
  [[ -n $ref ]] || {
    printf ''
    return 0
  }
  while IFS= read -r p; do
    base="${p##*/}"
    [[ $base == [0-9][0-9][0-9][0-9]_*.py ]] || continue
    num="${base%%_*}"
    if [[ -z $max || $num > $max ]]; then
      max="$num"
    fi
  done < <(git ls-tree -r --name-only "$ref" -- alembic/versions 2>/dev/null || true)
  printf '%s' "$max"
}

# 从 alembic 输出里抠 revision id（形如 0009_memory_medication）。
alembic_rev_of() {
  sed -nE 's/^([0-9]{4}_[A-Za-z0-9_]+).*/\1/p' | head -n1
}

# -----------------------------------------------------------------------------
# 1/8 前置检查
# -----------------------------------------------------------------------------
check_repo() {
  local top
  top="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  [[ -n $top ]] || die '当前目录不是 git 仓库（找不到 .git）'
  REPO_ROOT="$top"
  cd "$REPO_ROOT"

  ORIGINAL_SHA="$(git rev-parse HEAD)"
  ORIGINAL_SHORT="$(git rev-parse --short HEAD)"
  ORIGINAL_SUBJECT="$(git log -1 --pretty=%s)"
  ORIGINAL_DESC="$(git describe --tags --always 2>/dev/null || printf '%s' "$ORIGINAL_SHORT")"
  info "仓库根：$REPO_ROOT"
  if [[ $ORIGINAL_DESC == "$ORIGINAL_SHORT" ]]; then
    ok "当前 HEAD：$ORIGINAL_SHORT（无 tag 指向它）"
  else
    ok "当前 HEAD：$ORIGINAL_SHORT（$ORIGINAL_DESC）"
  fi
}

check_dirty() {
  local porcelain count
  porcelain="$(git -c core.quotepath=false status --porcelain)"
  if [[ -z $porcelain ]]; then
    ok '工作树干净（git status --porcelain 为空）'
    return 0
  fi
  count="$(printf '%s\n' "$porcelain" | wc -l | tr -d ' ')"
  if (( FORCE_DIRTY == 1 && ASSUME_YES == 1 )); then
    warn "工作树有 $count 处未提交改动，但给了 --yes --force-dirty：这些改动会被 git reset --hard 丢掉"
    printf '%s\n' "$porcelain" | sed -n '1,10s/^/              /p'
    (( count > 10 )) && note "……另有 $((count - 10)) 处未列出"
    return 0
  fi
  blocker "工作树有 $count 处未提交改动，回滚会执行 git reset --hard 把它们丢掉。先提交/丢弃，或确认后加 --yes --force-dirty"
  printf '%s\n' "$porcelain" | sed -n '1,10s/^/              /p'
  (( count > 10 )) && note "……另有 $((count - 10)) 处未列出"
  return 0
}

resolve_target() {
  local tag
  if [[ -n $REF_ARG ]]; then
    # 显式指定的 ref 解析不到 = 用法错误，dry-run 也一样（不是「计划能跑」，是指令本身错）。
    TARGET_SHA="$(git rev-parse --verify --quiet "${REF_ARG}^{commit}" || true)"
    if [[ -z $TARGET_SHA ]]; then
      die_usage "找不到 ref：$REF_ARG（不是 tag，也不是可解析的 commit）"
    fi
    TARGET_REF="$REF_ARG"
    ok "目标 ref（--ref 指定）：$TARGET_REF → ${TARGET_SHA:0:7}"
    return 0
  fi

  tag="$(git describe --tags --abbrev=0 HEAD 2>/dev/null || true)"
  if [[ -z $tag ]]; then
    TARGET_REF=''
    TARGET_SHA=''
    blocker '仓库里没有任何 tag，无法用「最近一个 tag」确定回滚目标：请用 --ref <tag|commit> 指定，或先打 tag（W8 冻结点打 v1.0-demo / final）'
    return 0
  fi
  TARGET_REF="$tag"
  TARGET_SHA="$(git rev-parse --verify "${tag}^{commit}")"
  ok "目标 ref（最近 tag）：$TARGET_REF → ${TARGET_SHA:0:7}"
}

check_ancestor() {
  [[ -n $TARGET_SHA ]] || return 0
  if [[ $TARGET_SHA == "$ORIGINAL_SHA" ]]; then
    warn "目标与当前 HEAD 是同一个提交，回滚是空操作（演练时这是正常的）"
    return 0
  fi
  if git merge-base --is-ancestor "$TARGET_SHA" HEAD; then
    ok '目标 ref 在当前分支的祖先链上（可以安全回退）'
    return 0
  fi
  if (( FORCE_REF == 1 )); then
    warn '目标 ref 不在 HEAD 的祖先链上，但给了 --force-ref：继续（历史可能被拉直/来自别的分支）'
    return 0
  fi
  blocker "目标 ref 不在当前分支的祖先链上（不是「回退」而是「跳转」）。确认无误后加 --force-ref"
}

check_tools() {
  local missing=()
  have git || missing+=(git)
  have docker || missing+=(docker)
  have curl || missing+=(curl)
  if ((${#missing[@]} > 0)); then
    blocker "缺少命令：${missing[*]}（回滚需要 git / docker / curl）"
  else
    ok 'git / docker / curl 均在 PATH 上'
  fi

  if (( DO_MIGRATE == 0 )); then
    info '按 --no-migrate 跳过迁移步骤，不检查 alembic'
    return 0
  fi
  if resolve_alembic; then
    ok "alembic 调用：${ALEMBIC_BIN[*]}"
  else
    blocker '找不到 alembic（uv / alembic / .venv 都不在）。装 uv 后重跑，或加 --no-migrate 只回代码'
  fi
}

step1_preflight() {
  step 1 '前置检查（仓库 / 脏工作树 / 目标 ref / 祖先链 / 工具链）'
  check_repo
  check_dirty
  resolve_target
  check_ancestor
  check_tools
}

# -----------------------------------------------------------------------------
# 2/8 打印现场
# -----------------------------------------------------------------------------
step2_show_context() {
  step 2 '回滚现场（最近 3 个提交 + 目标）'
  printf '    $ git log --oneline -3\n'
  git -c core.quotepath=false log --oneline -3 | sed 's/^/      /'
  if [[ -n $TARGET_SHA ]]; then
    local tsubject
    tsubject="$(git log -1 --pretty=%s "$TARGET_SHA")"
    info "目标：$TARGET_REF → ${TARGET_SHA:0:7}  $tsubject"
    info "跨度：$ORIGINAL_SHORT → ${TARGET_SHA:0:7}，$(git rev-list --count "$TARGET_SHA"..HEAD) 个提交将被退回"
  else
    warn '目标 ref 未解析，后续步骤只会打印不会执行'
  fi
}

# -----------------------------------------------------------------------------
# 3/8 停服务
# -----------------------------------------------------------------------------
# 【为什么绝不允许 docker compose down -v】——读这一条再改本文件：
#   `-v/--volumes` 会把 compose 编排里声明的**具名卷一起删除**。本项目 postgres 的数据盘
#   就是具名卷（docker-compose.yml 的 volumes: postgres_data → /var/lib/postgresql/data，
#   另有 redis_data、funasr_models）。删卷 = **整个数据库连表带数据一起蒸发**，
#   且 `git reset --hard` 回不来、`alembic upgrade/downgrade` 也回不来——迁移脚本只能重建
#   schema（空表），建不回行数据。回滚演练的目的恰恰是「保留数据退回旧版」，删卷等于把
#   演练变成一次数据销毁事故。同理禁止 `down --rmi all`（会连镜像一起清，下次要全量重拉）。
#   需要清库重来是**另一件事**（`docker compose down -v` 属人工手工操作，不进回滚脚本）。
# 另外：`docker compose down` 会停掉本机正在用的 anyu-postgres / anyu-redis，
#       所以本脚本在开发机上必须由人确认后手工执行，验证阶段只跑 --dry-run。
compose_down() {
  local argv=(docker compose down)
  guard_no_destructive_flags "${argv[@]}"
  run "${argv[@]}"
}

# 兜底闸：将来有人往 argv 里加 -v / --rmi，这里直接拒绝执行（而不是「相信注释」）。
guard_no_destructive_flags() {
  local a
  for a in "$@"; do
    case "$a" in
      -v | --volumes | -v=* | --volumes=* | --rmi | --rmi=*)
        die "拒绝执行：命令里出现 '$a'。down 加卷/镜像删除标志会销毁 postgres_data 数据卷（见脚本第 3 步注释）"
        ;;
    esac
  done
}

step3_stop() {
  step 3 '停服务（docker compose down，保留数据卷）'
  info '不删卷：postgres_data / redis_data / funasr_models 原样保留'
  compose_down
}

# -----------------------------------------------------------------------------
# 4/8 回代码
# -----------------------------------------------------------------------------
step4_reset_code() {
  step 4 '代码回到目标版本（git reset --hard）'
  if [[ -z $TARGET_SHA ]]; then
    warn '目标 ref 未解析：真实执行会在此中止，这里只打印形状'
    printf '    $ %s\n' 'git reset --hard <目标 ref>'
    return 0
  fi
  run git reset --hard "$TARGET_SHA"
}

# -----------------------------------------------------------------------------
# 5/8 回迁移
# -----------------------------------------------------------------------------
step5_migrate() {
  step 5 '迁移回退（alembic，--no-migrate 可跳）'

  if (( DO_MIGRATE == 0 )); then
    info '按 --no-migrate 跳过：只回代码，数据库 schema 保持不动'
    MIGRATION_VERDICT='跳过（--no-migrate）'
    return 0
  fi

  local cur_mig tgt_mig
  cur_mig="$(mig_number_from_dir alembic/versions)"
  tgt_mig="$(mig_number_from_ref "$TARGET_SHA")"
  info "当前工作树最新迁移号：${cur_mig:-<无>}"
  info "目标 ref 最新迁移号：${tgt_mig:-<无>}"
  if [[ -n $cur_mig && -n $tgt_mig ]]; then
    if [[ $tgt_mig == "$cur_mig" ]]; then
      info '静态预判：迁移号相同 → 预计不需要迁移动作'
    elif [[ $tgt_mig > $cur_mig ]]; then
      info "静态预判：目标更新（$tgt_mig > $cur_mig）→ 预计 alembic upgrade head"
    else
      info "静态预判：目标更旧（$tgt_mig < $cur_mig）→ 预计先 alembic downgrade 到目标 head"
    fi
  fi
  note '真实执行以 alembic current / alembic heads 为准，判定规则：'
  note '  A 库内版本 == 目标 head              → 跳过'
  note '  B 库内版本比目标 head 新（是其后代） → alembic downgrade <目标 head>'
  note '  C 库内版本比目标 head 旧（是其祖先） → alembic upgrade head'
  note '  D 两条链无关（号相同但 id 不同）      → 中止，交人工'

  # 迁移要连库，而第 3 步已经把 postgres 停了：先把数据层拉起来（不写全量服务清单，
  # 只起 alembic 依赖的 postgres + redis，服务名可用 ROLLBACK_DB_SERVICES 覆盖）。
  info "迁移需要数据库：先单独拉起数据层（$DB_SERVICES）"
  # shellcheck disable=SC2086  # DB_SERVICES 需要按空格拆成多个服务名
  run docker compose up -d $DB_SERVICES

  printf '    $ %s heads\n' "${ALEMBIC_BIN[*]:-alembic}"
  printf '    $ %s current\n' "${ALEMBIC_BIN[*]:-alembic}"

  if (( DRY_RUN == 1 )); then
    note '真实执行会按 A/B/C/D 决定下面两条中的一条：'
    printf '    $ %s downgrade <目标 head>    # 仅 B\n' "${ALEMBIC_BIN[*]:-alembic}"
    printf '    $ %s upgrade head            # 仅 C\n' "${ALEMBIC_BIN[*]:-alembic}"
    MIGRATION_VERDICT="dry-run 预判：${cur_mig:-?} → ${tgt_mig:-?}（未连库，未执行）"
    return 0
  fi

  local db_out db_rev target_head
  if ! db_out="$(wait_for_db)"; then
    MIGRATION_VERDICT='失败：数据库在超时内未就绪'
    die '数据库未就绪，无法执行迁移回退。先确认 postgres 健康（docker compose ps postgres），再重跑'
  fi
  db_rev="$(printf '%s' "$db_out" | alembic_rev_of)"
  target_head="$("${ALEMBIC_BIN[@]}" heads 2>/dev/null | alembic_rev_of)"
  if [[ -z $target_head ]]; then
    MIGRATION_VERDICT='中止：目标代码里没有 head 迁移'
    die '目标代码的 alembic heads 为空（alembic/versions 缺失或迁移链断裂），中止'
  fi

  info "库内当前版本：${db_rev:-<空，未迁移过>}"
  info "目标代码 head：$target_head"

  if [[ $db_rev == "$target_head" ]]; then
    ok 'A：库内版本已是目标 head，跳过迁移'
    MIGRATION_VERDICT="不变（${target_head}）"
    return 0
  fi

  local db_num tgt_num
  db_num="${db_rev%%_*}"
  tgt_num="${target_head%%_*}"
  [[ -n $db_num ]] || db_num='0000'

  if [[ $db_num > $tgt_num ]]; then
    info "B：库内（$db_num）比目标（$tgt_num）新 → downgrade 到 $target_head"
    run "${ALEMBIC_BIN[@]}" downgrade "$target_head"
    MIGRATION_VERDICT="$db_rev → $target_head（downgrade）"
  elif [[ $db_num < $tgt_num ]]; then
    info "C：库内（$db_num）比目标（$tgt_num）旧 → upgrade head"
    run "${ALEMBIC_BIN[@]}" upgrade head
    MIGRATION_VERDICT="$db_rev → $target_head（upgrade head）"
  else
    MIGRATION_VERDICT='中止：库内与目标迁移号相同但 id 不同（分支）'
    die "D：库内 $db_rev 与目标 $target_head 迁移号相同但 id 不同，迁移链已分叉，必须人工处理"
  fi
}

# 迁移前等库：alembic current 能跑通（连得上库）即认为就绪。
wait_for_db() {
  local n=1 out=''
  while ((n <= 10)); do
    if out="$("${ALEMBIC_BIN[@]}" current 2>&1)"; then
      printf '%s' "$out"
      return 0
    fi
    printf '    ... 数据库未就绪（第 %d/10 次）：%s\n' "$n" "$(printf '%s' "$out" | tail -n1)"
    sleep 3
    n=$((n + 1))
  done
  return 1
}

# -----------------------------------------------------------------------------
# 6/8 起服务
# -----------------------------------------------------------------------------
step6_up() {
  step 6 '按回滚后的代码重新起服务（docker compose up -d --build）'
  run docker compose up -d --build
}

# -----------------------------------------------------------------------------
# 7/8 探活
# -----------------------------------------------------------------------------
step7_health() {
  step 7 "健康检查（curl $HEALTH_URL，最多 $HEALTH_RETRIES 次 × ${HEALTH_INTERVAL}s）"
  print_cmd curl -fsS --max-time 5 "$HEALTH_URL"

  if (( DRY_RUN == 1 )); then
    note "真实执行：轮询至多为 $((HEALTH_RETRIES * HEALTH_INTERVAL))s；"
    note '失败则打印 docker compose logs --tail=50 backend 并 exit 1'
    HEALTH_VERDICT='（dry-run 未探活）'
    return 0
  fi

  local n=1 body=''
  while ((n <= HEALTH_RETRIES)); do
    if body="$(curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null)"; then
      ok "第 $n 次探活通过：$body"
      HEALTH_VERDICT="ok（第 $n 次，约 $(((n - 1) * HEALTH_INTERVAL))s 后恢复）"
      return 0
    fi
    if ((n % 5 == 0)); then
      printf '    ... 第 %d/%d 次未通过（已等 %ss）\n' "$n" "$HEALTH_RETRIES" "$((n * HEALTH_INTERVAL))"
    fi
    sleep "$HEALTH_INTERVAL"
    n=$((n + 1))
  done

  HEALTH_VERDICT="失败：连续 $HEALTH_RETRIES 次未通过"
  printf '    [FAIL]  健康检查连续 %d 次未通过，打印后端日志：\n' "$HEALTH_RETRIES"
  docker compose logs --tail=50 backend || true
  exit 1
}

# -----------------------------------------------------------------------------
# 8/8 摘要
# -----------------------------------------------------------------------------
step8_summary() {
  step 8 '回滚摘要'
  hr
  printf '  from        : %s（%s）%s\n' "$ORIGINAL_SHORT" "$ORIGINAL_DESC" "$ORIGINAL_SUBJECT"
  if [[ -n $TARGET_SHA ]]; then
    printf '  to          : %s（%s）%s\n' "${TARGET_SHA:0:7}" "$TARGET_REF" "$(git log -1 --pretty=%s "$TARGET_SHA")"
  else
    printf '  to          : <未解析>\n'
  fi
  printf '  migration   : %s\n' "$MIGRATION_VERDICT"
  printf '  health      : %s\n' "$HEALTH_VERDICT"
  printf '  数据卷      : 保留（down 未加 -v）\n'
  if [[ -n $TARGET_SHA && $TARGET_SHA != "$ORIGINAL_SHA" ]]; then
    printf '  反向操作    : git reset --hard %s   # 需要时手工前进回原提交\n' "$ORIGINAL_SHA"
  fi
  hr

  if (( DRY_RUN == 1 )); then
    printf '\n[SUMMARY] DRY-RUN：一条命令都没有执行，仓库与容器零改动。\n'
    if ((${#BLOCKERS[@]} > 0)); then
      printf '          真实执行会被下面 %d 条前置检查挡住：\n' "${#BLOCKERS[@]}"
      local b
      for b in "${BLOCKERS[@]}"; do
        printf '            - %s\n' "$b"
      done
      printf '          解除办法：逐条处理上表；或者先只演练 --dry-run（它不需要仓库干净）。\n'
    else
      printf '          前置检查全过：把 --dry-run 去掉即可真实执行（仍需 --yes 或交互确认）。\n'
    fi
    printf '\n[SUMMARY] 只做了 dry-run 的步骤：3(down) / 4(reset) / 5(alembic) / 6(up) / 7(health)\n'
    return 0
  fi

  printf '\n  [DONE] 回滚完成：%s → %s\n' "$ORIGINAL_SHORT" "${TARGET_SHA:0:7}"
}

# -----------------------------------------------------------------------------
# 自保：第 4 步 git reset --hard 会把本文件也一起改回旧版本（甚至删掉）。
# bash 是「边读边执行」脚本的，执行途中源文件被换掉会读到半截（未定义行为）。
# 所以真实执行前，先把脚本自身复制到临时目录再 exec 一次，让内存里的代码与工作树解耦。
# -----------------------------------------------------------------------------
ensure_self_contained() {
  if [[ -n ${ROLLBACK_SELF_COPIED:-} ]]; then
    if [[ -n ${ROLLBACK_SELF_TMP:-} ]]; then
      # shellcheck disable=SC2064  # 故意在退出时才展开，避免提前吃掉变量
      trap 'rm -f "$ROLLBACK_SELF_TMP" 2>/dev/null || true' EXIT
    fi
    return 0
  fi
  local tmp=''
  tmp="$(mktemp "${TMPDIR:-/tmp}/anyu-rollback.XXXXXX.sh" 2>/dev/null || true)"
  if [[ -z $tmp ]]; then
    warn 'mktemp 不可用，未能自复制；若目标版本的 scripts/rollback.sh 与此处不同，注意行为可能漂移'
    return 0
  fi
  cp -- "$0" "$tmp"
  info "已把脚本自身复制到 $tmp 后重入（防止 git reset --hard 改掉正在执行的这个文件）"
  # 必须用「当前正在跑的这个 bash」重入，不能用裸 bash：
  # Windows 上 PATH 里 /c/WINDOWS/system32/bash（WSL 的 bash.exe）常排在 /usr/bin/bash 之前，
  # 裸 bash 会拉起 WSL bash，它把 C:/... 当成 Linux 路径 → "No such file or directory"（exit 127）。
  # $BASH 永远是当前解释器自身的路径，跨 Git Bash / WSL / Linux 都成立。
  ROLLBACK_SELF_COPIED=1 ROLLBACK_SELF_TMP="$tmp" exec "${BASH:-/usr/bin/bash}" "$tmp" "$@"
}

# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------
main() {
  parse_args "$@"

  # dry-run 不动工作树，无需自复制；真实执行才保护。
  if ((DRY_RUN == 0)); then
    ensure_self_contained "$@"
  fi

  hr
  if (( DRY_RUN == 1 )); then
    printf '安语回滚 · DRY-RUN（只打印，不执行）\n'
  else
    printf '安语回滚 · 真实执行\n'
  fi
  hr

  step1_preflight

  # 真实执行才拦 blocker；dry-run 继续把整份计划打完，最后统一汇报会被什么挡住。
  if (( DRY_RUN == 0 )); then
    if ((${#BLOCKERS[@]} > 0)); then
      printf '\n[FAIL] 前置检查未通过，已中止（一条命令都没执行）：\n'
      local b
      for b in "${BLOCKERS[@]}"; do
        printf '  - %s\n' "$b"
      done
      exit 1
    fi
    if (( ASSUME_YES == 0 )); then
      if [[ ! -t 0 ]]; then
        die '非交互环境必须显式给 --yes（回滚不可无人值守地默认执行）'
      fi
      printf '\n即将把 %s 回滚到 %s，并重建容器；未提交改动会丢失。继续？[y/N] ' \
        "$ORIGINAL_SHORT" "${TARGET_SHA:0:7}"
      local ans=''
      read -r ans || ans=''
      case "$ans" in
        y | Y | yes | YES) ;;
        *) printf '\n[取消] 用户未确认，未执行任何操作\n'; exit 1 ;;
      esac
    fi
  fi

  step2_show_context
  step3_stop
  step4_reset_code
  step5_migrate
  step6_up
  step7_health
  step8_summary
}

main "$@"
