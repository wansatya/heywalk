#!/usr/bin/env bash
#
# HeyWalk one-line installer.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/wansatya/heywalk/main/install.sh | bash
#
# What it does:
#   1. Installs git, curl, and Docker (with the compose plugin) when missing.
#   2. Clones this repo to ~/.heywalk (or updates it when re-run).
#   3. Asks for an optional Meta API key (AI generation/correction only;
#      the simulator, starter robot, and runs work without one).
#   4. Builds the images and starts the stack detached.
#
# When it finishes, open: http://localhost:8021
#
# Environment knobs (all optional):
#   INSTALL_DIR    checkout location              (default: $HOME/.heywalk)
#   WEB_PORT       browser port                   (default: 8021)
#   REF            git branch or tag              (default: main)
#   MODEL_API_KEY  Meta API key (skips the key prompt when set)
#   REBUILD        1 = rebuild the runner image   (default: reuse if present)
#
# Flags: --help | --dry-run | --rebuild | --dir PATH | --port PORT | --ref REF

set -euo pipefail

REPO_URL="https://github.com/wansatya/heywalk"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.heywalk}"
WEB_PORT="${WEB_PORT:-8021}"
REF="${REF:-main}"
REBUILD="${REBUILD:-0}"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage:
  curl -fsSL https://raw.githubusercontent.com/wansatya/heywalk/main/install.sh | bash
  bash install.sh [--rebuild] [--dry-run] [--dir PATH] [--port PORT] [--ref REF]

Installs Docker (if missing), clones/updates heywalk, optionally stores a
Meta API key for AI generation, builds the images, and starts the stack.
Then open: http://localhost:8021 (or your --port).

Environment (alternatives to flags):
  INSTALL_DIR, WEB_PORT, REF, REBUILD=1, MODEL_API_KEY (skips the key prompt)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --rebuild) REBUILD=1; shift ;;
    --dir) INSTALL_DIR="${2:?--dir needs a path}"; shift 2 ;;
    --port) WEB_PORT="${2:?--port needs a port}"; shift 2 ;;
    --ref) REF="${2:?--ref needs a branch or tag}"; shift 2 ;;
    *) echo "unknown flag: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -t 1 ]; then
  GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RED=$'\e[31m'; DIM=$'\e[2m'; RESET=$'\e[0m'
else
  GREEN=''; YELLOW=''; RED=''; DIM=''; RESET=''
fi
say() { printf '%s==> %s%s\n' "$GREEN" "$*" "$RESET"; }
warn() { printf '%swarning:%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die() { printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

case "$WEB_PORT" in ''|*[!0-9]*) die "WEB_PORT must be a number, got '$WEB_PORT'" ;; esac

OS="$(uname -s)"
case "$OS" in Linux|Darwin) ;; *) die "unsupported OS: $OS (Linux and macOS only)" ;; esac

if [ "$(id -u)" -eq 0 ]; then SUDO=""
elif command -v sudo >/dev/null 2>&1; then SUDO="sudo"
else die "need root privileges and sudo is not installed" ; fi
# NOTE: $SUDO is intentionally unquoted below (empty for root).

pkg_install() {
  # $1 = command to check for, $@ = packages to install when missing.
  local cmd="$1"; shift
  command -v "$cmd" >/dev/null 2>&1 && return 0
  if [ "$OS" = "Darwin" ]; then
    command -v brew >/dev/null 2>&1 || die "install Homebrew (https://brew.sh) and re-run"
    say "installing $* via Homebrew..."
    brew install "$@"
  elif command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq "$@"
  elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y -q "$@"
  elif command -v yum >/dev/null 2>&1; then
    $SUDO yum install -y -q "$@"
  elif command -v pacman >/dev/null 2>&1; then
    $SUDO pacman -Sy --noconfirm "$@"
  else
    die "no supported package manager (apt/dnf/yum/pacman); install $* by hand and re-run"
  fi
}

docker_up() {
  docker info >/dev/null 2>&1 \
    || { [ -n "$SUDO" ] && $SUDO docker info >/dev/null 2>&1; }
}

port_in_use() {
  # True when something accepts TCP on 127.0.0.1:$1.
  ( : </dev/tcp/127.0.0.1/"$1" ) 2>/dev/null
}

env_has_key() {
  [ -f .env ] && grep -qE '^MODEL_API_KEY=[^[:space:]]' .env
}

write_env_key() {
  # Store $1 as MODEL_API_KEY in ./.env (mode 600), keeping other lines.
  # The key is never printed.
  [ -f .env ] || : > .env
  grep -v '^MODEL_API_KEY=' .env > .env.tmp 2>/dev/null || true
  mv -f .env.tmp .env
  printf 'MODEL_API_KEY=%s\n' "$1" >> .env
  chmod 600 .env
}

ensure_docker_linux() {
  if ! command -v docker >/dev/null 2>&1; then
    say "installing Docker Engine (get.docker.com)..."
    curl -fsSL https://get.docker.com | $SUDO sh
  fi
  if ! docker_up; then
    say "starting the Docker daemon..."
    if command -v systemctl >/dev/null 2>&1; then
      $SUDO systemctl enable --now docker
    elif command -v service >/dev/null 2>&1; then
      $SUDO service docker start
    else
      die "could not start the Docker daemon automatically; start it and re-run"
    fi
    docker_up || die "Docker daemon still unreachable after starting it"
  fi
  if docker info >/dev/null 2>&1; then DOCKER="docker"
  elif [ -n "$SUDO" ] && $SUDO docker info >/dev/null 2>&1; then
    DOCKER="$SUDO docker"
    warn "docker needs sudo here; continuing with sudo (add yourself to the docker group to avoid it)"
  else
    die "cannot talk to the Docker daemon"
  fi
}

ensure_docker_macos() {
  if ! command -v docker >/dev/null 2>&1; then
    command -v brew >/dev/null 2>&1 || die "install Homebrew (https://brew.sh) and re-run"
    say "installing docker + colima via Homebrew..."
    brew install docker docker-compose colima
  fi
  if ! docker info >/dev/null 2>&1; then
    if command -v colima >/dev/null 2>&1; then
      say "starting colima..."
      colima start
      docker info >/dev/null 2>&1 || die "Docker is still unreachable after 'colima start'"
    else
      die "Docker daemon not reachable; start Docker Desktop (or install colima) and re-run"
    fi
  fi
  DOCKER="docker"
}

dry_run() {
  echo "dry run — nothing will be installed or changed."
  echo "os: $OS"
  if [ -d "$INSTALL_DIR/.git" ]; then echo "checkout: update $INSTALL_DIR ($REF)"
  elif [ -e "$INSTALL_DIR" ]; then echo "checkout: BLOCKED ($INSTALL_DIR exists, not a git checkout)"
  else echo "checkout: fresh clone of $REPO_URL ($REF) into $INSTALL_DIR"; fi
  echo "port: $WEB_PORT (in use: $(port_in_use "$WEB_PORT" && echo yes || echo no))"
  for cmd in git curl docker; do
    if command -v "$cmd" >/dev/null 2>&1; then echo "$cmd: present"
    else echo "$cmd: MISSING (would install)"; fi
  done
  # Plain docker only here: a dry run must never trigger a sudo prompt.
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    if docker compose version >/dev/null 2>&1; then echo "compose: present"
    else echo "compose: MISSING"; fi
    if docker image inspect heywalk-runner:jazzy >/dev/null 2>&1; then
      echo "runner image: present (would reuse)"
    else echo "runner image: missing (would build; slow on first install)"; fi
  else
    echo "compose: unknown (docker not reachable without sudo)"
    echo "runner image: unknown (docker not reachable without sudo)"
  fi
  if [ -n "${MODEL_API_KEY:-}" ]; then echo "ai key: would use \$MODEL_API_KEY"
  elif [ -f "$INSTALL_DIR/.env" ] && ( cd "$INSTALL_DIR" && env_has_key ); then
    echo "ai key: already stored in $INSTALL_DIR/.env"
  elif { true <>/dev/tty; } 2>/dev/null; then echo "ai key: would prompt (Enter skips)"
  else echo "ai key: would skip (no terminal)"; fi
  avail_kb="$(df -kP / 2>/dev/null | awk 'NR==2 {print $4}')"
  case "$avail_kb" in ''|*[!0-9]*) echo "disk: unknown" ;;
  *) echo "disk: $((avail_kb / 1024 / 1024)) GiB free on /" ;; esac
}

checkout() {
  if [ -d "$INSTALL_DIR/.git" ]; then
    say "updating $INSTALL_DIR ($REF)..."
    cd "$INSTALL_DIR"
    git fetch --quiet origin || die "git fetch failed (network or remote gone?)"
    git checkout --quiet "$REF" || die "cannot check out '$REF'"
    git pull --ff-only --quiet origin "$REF" \
      || die "cannot fast-forward (local commits?); stash them or clone fresh"
  elif [ -e "$INSTALL_DIR" ]; then
    die "$INSTALL_DIR exists and is not a git checkout; move it aside or pass --dir"
  else
    say "cloning $REPO_URL ($REF) into $INSTALL_DIR..."
    git clone --quiet --branch "$REF" --depth 1 "$REPO_URL" "$INSTALL_DIR" \
      || die "git clone failed"
    cd "$INSTALL_DIR"
  fi
}

prepare_staging() {
  # The API stages per-run workspaces on a host path that must match inside
  # the container (see docker-compose.yml); pre-create it world-writable so
  # the unprivileged api user can write it.
  mkdir -p /tmp/heywalk-staging
  chmod 777 /tmp/heywalk-staging 2>/dev/null || {
    { [ -n "$SUDO" ] && $SUDO chmod 777 /tmp/heywalk-staging; } 2>/dev/null \
      || warn "cannot chmod /tmp/heywalk-staging; runs may fail (the UI still loads)"
  }
  if command -v colima >/dev/null 2>&1 \
      && colima status 2>/dev/null | grep -qi "running"; then
    # On colima the daemon's "host" is the VM, so prepare the path there too.
    colima ssh -- sudo sh -c \
      'mkdir -p /tmp/heywalk-staging && chmod 777 /tmp/heywalk-staging' \
      >/dev/null 2>&1 \
      || warn "colima VM staging setup failed; runs may fail (the UI still loads)"
  fi
}

check_disk() {
  avail_kb="$(df -kP / 2>/dev/null | awk 'NR==2 {print $4}')"
  case "$avail_kb" in ''|*[!0-9]*) return 0 ;; esac
  if [ "$avail_kb" -lt 15728640 ]; then
    warn "only $((avail_kb / 1024 / 1024)) GiB free on /; image builds want ~15 GiB"
  fi
}

setup_api_key() {
  # KEY_STATE is consumed by the final banner: env | stored | prompt | missing.
  if [ -n "${MODEL_API_KEY:-}" ]; then
    write_env_key "$MODEL_API_KEY"
    say "Meta API key saved from \$MODEL_API_KEY to .env (mode 600)"
    KEY_STATE="env"
  elif env_has_key; then
    say "using the Meta API key already stored in .env"
    KEY_STATE="stored"
  elif { true <>/dev/tty; } 2>/dev/null; then
    printf '\n%sMeta API key (optional, needed only for AI generate/fix).%s\n' "$DIM" "$RESET"
    printf 'Press Enter to skip — the simulator and runs work without one.\n'
    key_input=""
    IFS= read -r -s -p "MODEL_API_KEY: " key_input < /dev/tty || true
    printf '\n'
    if [ -n "$key_input" ]; then
      write_env_key "$key_input"
      say "Meta API key saved to .env (mode 600)"
      KEY_STATE="prompt"
    else
      say "skipped — continuing without an AI key"
      KEY_STATE="missing"
    fi
    unset key_input
  else
    say "no terminal for the optional key prompt — continuing without an AI key"
    KEY_STATE="missing"
  fi
}

main() {
  trap 'die "installer failed (line $LINENO)"' ERR
  if [ "$DRY_RUN" = "1" ]; then dry_run; return 0; fi

  pkg_install git git
  pkg_install curl curl
  if [ "$OS" = "Darwin" ]; then ensure_docker_macos; else ensure_docker_linux; fi
  $DOCKER compose version >/dev/null 2>&1 \
    || die "the docker compose plugin is missing; update Docker and re-run"

  checkout
  if port_in_use "$WEB_PORT"; then
    if $DOCKER compose ps --status running --services 2>/dev/null | grep -qx "web"; then
      say "port $WEB_PORT already serves this stack; updating it in place"
    else
      die "port $WEB_PORT is in use by something else; re-run with WEB_PORT=<free-port>"
    fi
  fi
  prepare_staging
  check_disk
  setup_api_key

  export WEB_PORT
  if [ "$REBUILD" = "1" ] \
      || ! $DOCKER image inspect heywalk-runner:jazzy >/dev/null 2>&1; then
    say "building the isolated-runner image (slow on first install, up to ~40 min on small machines)..."
    $DOCKER compose --profile runner-build build runner
  else
    say "reusing the existing heywalk-runner:jazzy image (--rebuild to force)"
  fi
  say "building and starting the stack..."
  $DOCKER compose up --build -d

  say "waiting for http://localhost:${WEB_PORT} ..."
  healthy=0
  tries=0
  while [ "$tries" -lt 60 ]; do
    if curl -fs -m 3 -o /dev/null "http://127.0.0.1:${WEB_PORT}/health" 2>/dev/null; then
      healthy=1
      break
    fi
    tries=$((tries + 1))
    sleep 3
  done
  if [ "$healthy" != "1" ]; then
    warn "the stack did not become healthy in time; recent logs:"
    $DOCKER compose logs --tail=40 web api 2>&1 | tail -60 || true
    die "start failed — see the logs above (or run: cd $INSTALL_DIR && $DOCKER compose logs -f)"
  fi

  printf '\n%s============================================%s\n' "$GREEN" "$RESET"
  printf '%s  HeyWalk is running: http://localhost:%s%s\n' "$GREEN" "$WEB_PORT" "$RESET"
  printf '%s============================================%s\n' "$GREEN" "$RESET"
  printf '  Code:   %s\n' "$INSTALL_DIR"
  printf '  Logs:   cd %s && %s compose logs -f\n' "$INSTALL_DIR" "$DOCKER"
  printf '  Stop:   cd %s && %s compose down\n' "$INSTALL_DIR" "$DOCKER"
  printf '  Update: re-run this installer\n'
  case "${KEY_STATE:-missing}" in
    env|stored|prompt) printf '  AI key: configured (stored in %s/.env)\n' "$INSTALL_DIR" ;;
    *) printf '  AI key: not set — the simulator works; to enable AI later, add\n'
       printf '          MODEL_API_KEY=<key> to %s/.env, then:\n' "$INSTALL_DIR"
       printf '          cd %s && %s compose up -d\n' "$INSTALL_DIR" "$DOCKER" ;;
  esac
}

# Sourcing the file (e.g. in a test) defines the functions without running.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi

