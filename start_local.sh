#!/usr/bin/env bash
# IT-Copilot 一键开发启动器：网关(8082) + API(8001) + Vue(5173)。
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI_IT_COPILOT_PORT="${IT_COPILOT_PORT-}"
CLI_IT_COPILOT_FRONTEND_PORT="${IT_COPILOT_FRONTEND_PORT-}"
CLI_GATEWAY_PORT="${GATEWAY_PORT-}"

if [[ -f "$HERE/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$HERE/.env"
  set +a
fi

# .env 提供默认值，但显式传入的环境变量优先级更高。
[[ -n "$CLI_IT_COPILOT_PORT" ]] && IT_COPILOT_PORT="$CLI_IT_COPILOT_PORT"
[[ -n "$CLI_IT_COPILOT_FRONTEND_PORT" ]] && IT_COPILOT_FRONTEND_PORT="$CLI_IT_COPILOT_FRONTEND_PORT"
[[ -n "$CLI_GATEWAY_PORT" ]] && GATEWAY_PORT="$CLI_GATEWAY_PORT"
export IT_COPILOT_PORT="${IT_COPILOT_PORT:-8001}"
export IT_COPILOT_FRONTEND_PORT="${IT_COPILOT_FRONTEND_PORT:-5173}"
export GATEWAY_PORT="${GATEWAY_PORT:-8082}"
export NO_PROXY="127.0.0.1,localhost"; export no_proxy="$NO_PROXY"

# 优先使用项目虚拟环境，其次使用本机已有的工作虚拟环境；绝不优先使用系统 Python。
resolve_python() {
  local candidate
  local candidates=(
    "${IT_COPILOT_PYTHON:-}"
    "$HERE/.venv/bin/python"
    "$HERE/work/venv/bin/python"
  )
  for candidate in "${candidates[@]}"; do
    [[ -n "$candidate" && -x "$candidate" ]] && { printf '%s\n' "$candidate"; return 0; }
  done
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "$(command -v python3)"
    return 0
  fi
  echo "未找到 Python 3，请安装 Python 3.11+。" >&2
  return 1
}

PYTHON_BIN="$(resolve_python)"
if [[ "$PYTHON_BIN" == "$(command -v python3 2>/dev/null || true)" && ! -x "$HERE/.venv/bin/python" ]]; then
  echo "警告：未发现项目虚拟环境，将使用系统 Python：$PYTHON_BIN" >&2
fi

# 首次运行时创建项目虚拟环境；已有 work/venv 会直接复用。
if [[ "$PYTHON_BIN" == "$(command -v python3 2>/dev/null || true)" && ! -d "$HERE/.venv" ]]; then
  echo "创建项目虚拟环境 $HERE/.venv ..."
  python3 -m venv "$HERE/.venv"
  PYTHON_BIN="$HERE/.venv/bin/python"
fi

ensure_python_deps() {
  if ! "$PYTHON_BIN" -c 'import fastapi, uvicorn, anthropic, redis, chromadb, dotenv' >/dev/null 2>&1; then
    echo "Python 依赖不完整，使用 $PYTHON_BIN 安装 requirements.txt ..."
    "$PYTHON_BIN" -m pip install --disable-pip-version-check -r "$HERE/requirements.txt"
  fi
}

ensure_redis() {
  command -v docker >/dev/null 2>&1 || { echo "未找到 Docker，无法启动必需的 Redis 容器。" >&2; return 1; }
  docker info >/dev/null 2>&1 || { echo "Docker 未运行，无法启动 it-copilot-redis。" >&2; return 1; }
  local container="${IT_COPILOT_REDIS_CONTAINER:-it-copilot-redis}"
  local image="${IT_COPILOT_REDIS_IMAGE:-redis:7-alpine}"
  local password="${REDIS_PASSWORD:-it-copilot123}"
  if docker container inspect "$container" >/dev/null 2>&1; then
    local state
    state="$(docker inspect -f '{{.State.Running}}' "$container")"
    [[ "$state" == "true" ]] || { echo "启动 Redis 容器 $container ..."; docker start "$container" >/dev/null; }
  else
    echo "创建 Redis 容器 $container ..."
    docker run -d --name "$container" --restart unless-stopped \
      -p 127.0.0.1:6379:6379 \
      -v it-copilot-redis-data:/data \
      "$image" redis-server --appendonly yes --requirepass "$password" >/dev/null
  fi
  local i
  for i in {1..20}; do
    if docker exec "$container" redis-cli -a "$password" --no-auth-warning PING 2>/dev/null | grep -qx PONG; then
      echo "Redis 已就绪：$container (127.0.0.1:6379)"
      return 0
    fi
    sleep 0.5
  done
  echo "Redis 容器已启动但认证/健康检查失败：$container。请检查 REDIS_PASSWORD 与容器启动命令。" >&2
  return 1
}

free_port() {
  local port="$1" pids pid
  pids="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  [[ -z "$pids" ]] && return 0
  echo "清理端口 $port 上的旧进程：$pids"
  for pid in $pids; do kill "$pid" 2>/dev/null || true; done
  sleep 0.5
  for pid in $pids; do kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true; done
}

ensure_python_deps
ensure_redis
free_port "$GATEWAY_PORT"
free_port "$IT_COPILOT_PORT"
free_port "$IT_COPILOT_FRONTEND_PORT"

export IT_COPILOT_PYTHON="$PYTHON_BIN"
export PYTHONPATH="$HERE"
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-http://127.0.0.1:${GATEWAY_PORT}}"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-sk-local}"
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-deepseek-chat}"
export REDIS_URL="${REDIS_URL:-redis://:it-copilot123@127.0.0.1:6379/0}"
export CHROMA_PERSIST_DIRECTORY="${CHROMA_PERSIST_DIRECTORY:-$HERE/data/chroma}"
export IT_COPILOT_SKILLS_DIR="${IT_COPILOT_SKILLS_DIR:-$HERE/skills}"
mkdir -p "$HERE/logs" "$CHROMA_PERSIST_DIRECTORY"

GATEWAY_PID=""; API_PID=""; FRONTEND_PID=""
cleanup() {
  trap - INT TERM EXIT
  for pid in "$FRONTEND_PID" "$API_PID" "$GATEWAY_PID"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
}
trap cleanup INT TERM EXIT

if [[ -f "$HERE/gateway/.env.gateway" || -n "${OPENAI_API_KEY:-}" ]]; then
  IT_COPILOT_PYTHON="$PYTHON_BIN" "$HERE/gateway/run_gateway.sh" >"$HERE/logs/gateway.log" 2>&1 & GATEWAY_PID=$!
else
  echo "未找到 gateway/.env.gateway，网关不会启动。请先复制 gateway/.env.gateway.example 并填写模型配置。" >&2
fi

IT_COPILOT_PYTHON="$PYTHON_BIN" "$HERE/run_local.sh" >"$HERE/logs/api.log" 2>&1 & API_PID=$!

NPM_CACHE="$HERE/.npm-cache"
mkdir -p "$NPM_CACHE"
chmod -R u+rwX "$NPM_CACHE" 2>/dev/null || true
export npm_config_cache="$NPM_CACHE"
cd "$HERE/frontend"
if [[ ! -x "$HERE/frontend/node_modules/.bin/vite" ]]; then
  echo "前端依赖不完整，使用项目缓存 $NPM_CACHE 安装..."
  npm cache clean --force --cache "$NPM_CACHE" >/dev/null 2>&1 || true
  npm ci --no-audit --no-fund
fi

npm run dev -- --host 127.0.0.1 --port "$IT_COPILOT_FRONTEND_PORT" >"$HERE/logs/frontend.log" 2>&1 & FRONTEND_PID=$!

echo "IT-Copilot 已启动："
echo "  前端: http://127.0.0.1:${IT_COPILOT_FRONTEND_PORT}"
echo "  API:  http://127.0.0.1:${IT_COPILOT_PORT}"
echo "  日志: $HERE/logs/{gateway,api,frontend}.log"
wait "$API_PID"
