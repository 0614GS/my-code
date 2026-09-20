#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"

usage() {
    cat <<'EOF'
用法：scripts/run_harbor_swebench.sh [Harbor 额外参数...]

默认读取仓库根目录的 .env.harbor，并运行一个 SWE-bench Verified smoke task。
可用 HARBOR_ENV_FILE 指向其他 env 文件。命令行末尾的参数会原样追加给 harbor run，
例如：

  scripts/run_harbor_swebench.sh --include-task-name 'django__django-*'
  scripts/run_harbor_swebench.sh --dry-run

首次使用：

  cp .env.harbor.example .env.harbor
  # 编辑 .env.harbor，填写 MYCODE_API_KEY、MYCODE_MODEL、MYCODE_PROTOCOL
  scripts/run_harbor_swebench.sh
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi

env_file="${HARBOR_ENV_FILE:-$repo_root/.env.harbor}"
if [[ ! -f "$env_file" ]]; then
    echo "错误：找不到 Harbor 环境文件：$env_file" >&2
    echo "请先执行：cp .env.harbor.example .env.harbor" >&2
    exit 2
fi

# .env.harbor.example 使用 shell 兼容的 KEY=VALUE 格式。加载后不打印任何变量值。
set -a
# shellcheck disable=SC1090
source "$env_file"
set +a

if ! command -v uv >/dev/null 2>&1; then
    echo "错误：找不到 uv。" >&2
    exit 2
fi

: "${MYCODE_MODEL:?请在 .env.harbor 中设置 MYCODE_MODEL}"

if [[ -z "${MYCODE_API_KEY:-}" ]]; then
    echo "错误：请设置 MYCODE_API_KEY。" >&2
    exit 2
fi

dataset="${HARBOR_DATASET:-swe-bench/swe-bench-verified}"
protocol="${MYCODE_PROTOCOL:-}"
artifact_dir="${HARBOR_ARTIFACT_DIR:-/tmp/mycode-harbor-artifacts}"
n_tasks="${HARBOR_N_TASKS:-1}"
n_attempts="${HARBOR_N_ATTEMPTS:-1}"
n_concurrent="${HARBOR_N_CONCURRENT:-1}"
job_name="${HARBOR_JOB_NAME:-mycode-swe-smoke}"
jobs_dir="${HARBOR_JOBS_DIR:-jobs}"
rebuild_artifacts="${HARBOR_REBUILD_ARTIFACTS:-1}"
dry_run="${HARBOR_DRY_RUN:-0}"
assume_yes="${HARBOR_YES:-0}"

for assignment in \
    "HARBOR_N_TASKS=$n_tasks" \
    "HARBOR_N_ATTEMPTS=$n_attempts" \
    "HARBOR_N_CONCURRENT=$n_concurrent"; do
    name="${assignment%%=*}"
    value="${assignment#*=}"
    if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
        echo "错误：$name 必须是正整数，实际为 $value。" >&2
        exit 2
    fi
done

case "$protocol" in
    anthropic-messages | openai-responses) ;;
    *)
        echo "错误：MYCODE_PROTOCOL 必须是 anthropic-messages 或 openai-responses。" >&2
        exit 2
        ;;
esac

export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/my-code-uv-cache}"
export PYTHONPATH="$repo_root${PYTHONPATH:+:$PYTHONPATH}"

cd "$repo_root"

base_url_host=""
if [[ -n "${MYCODE_BASE_URL:-}" ]]; then
    base_url_host="$(
        uv run python -c \
            'import sys; from urllib.parse import urlparse; print(urlparse(sys.argv[1]).hostname or "")' \
            "$MYCODE_BASE_URL"
    )"
    if [[ -z "$base_url_host" ]]; then
        echo "错误：MYCODE_BASE_URL 不是有效的绝对 URL。" >&2
        exit 2
    fi
fi

if [[ "$rebuild_artifacts" == "1" ]]; then
    uv run python scripts/build_harbor_artifacts.py "$artifact_dir"
elif [[ ! -f "$artifact_dir/manifest.json" ]]; then
    echo "错误：HARBOR_REBUILD_ARTIFACTS=0，但缺少 $artifact_dir/manifest.json。" >&2
    exit 2
fi

command=(
    uv run harbor run
    --dataset "$dataset"
    --agent integrations.harbor.agent:MyCodeAgent
    --model "$MYCODE_MODEL"
    --agent-kwarg "artifact_dir=$artifact_dir"
    --env-file "$env_file"
    --n-tasks "$n_tasks"
    --n-attempts "$n_attempts"
    --n-concurrent "$n_concurrent"
    --job-name "$job_name"
    --jobs-dir "$jobs_dir"
)

if [[ -n "$base_url_host" ]]; then
    command+=(--allow-agent-host "$base_url_host")
fi
if [[ -n "${HARBOR_ALLOW_AGENT_HOST:-}" && "$HARBOR_ALLOW_AGENT_HOST" != "$base_url_host" ]]; then
    command+=(--allow-agent-host "$HARBOR_ALLOW_AGENT_HOST")
fi
if [[ "$dry_run" == "1" ]]; then
    command+=(--dry-run)
fi
if [[ "$assume_yes" == "1" ]]; then
    command+=(--yes)
fi

command+=("$@")

echo "运行 Harbor：dataset=$dataset model=$MYCODE_MODEL protocol=$protocol tasks=$n_tasks attempts=$n_attempts concurrent=$n_concurrent"
"${command[@]}"
