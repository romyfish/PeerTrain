#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

PA_REMOTE="${PA_REMOTE:-origin}"
PA_BRANCH="${PA_BRANCH:-main}"
PA_VENV_NAME="${PA_VENV_NAME:-peertrain-venv}"
PA_WSGI_PATH="${PA_WSGI_PATH:-/var/www/${USER}_pythonanywhere_com_wsgi.py}"

PA_INSTALL_DEPS=1
PA_RUN_CHECK=1
PA_RUN_MIGRATE=1
PA_RUN_STATIC=1
PA_RUN_SEED=0
PA_RUN_RELOAD=1

log() {
  printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

usage() {
  cat <<EOF
PeerTrain PythonAnywhere one-click update script

Usage:
  bash scripts/pythonanywhere_update.sh [options]

Options:
  --seed              Run python manage.py seed_peertrain after migrate
  --skip-install      Skip pip install -r requirements.txt
  --skip-check        Skip python manage.py check
  --skip-migrate      Skip python manage.py migrate
  --skip-static       Skip python manage.py collectstatic --noinput
  --skip-reload       Skip touching the WSGI file
  --venv NAME         Override the virtualenv name (default: ${PA_VENV_NAME})
  --branch NAME       Override the git branch (default: ${PA_BRANCH})
  --remote NAME       Override the git remote (default: ${PA_REMOTE})
  --wsgi-path PATH    Override the WSGI file path (default: ${PA_WSGI_PATH})
  -h, --help          Show this help message

Environment overrides:
  PA_REMOTE
  PA_BRANCH
  PA_VENV_NAME
  PA_WSGI_PATH

Examples:
  bash scripts/pythonanywhere_update.sh
  bash scripts/pythonanywhere_update.sh --seed
  bash scripts/pythonanywhere_update.sh --skip-install
  PA_WSGI_PATH=/var/www/yourusername_pythonanywhere_com_wsgi.py bash scripts/pythonanywhere_update.sh
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --seed)
      PA_RUN_SEED=1
      ;;
    --skip-install)
      PA_INSTALL_DEPS=0
      ;;
    --skip-check)
      PA_RUN_CHECK=0
      ;;
    --skip-migrate)
      PA_RUN_MIGRATE=0
      ;;
    --skip-static)
      PA_RUN_STATIC=0
      ;;
    --skip-reload)
      PA_RUN_RELOAD=0
      ;;
    --venv)
      shift
      PA_VENV_NAME="$1"
      ;;
    --branch)
      shift
      PA_BRANCH="$1"
      ;;
    --remote)
      shift
      PA_REMOTE="$1"
      ;;
    --wsgi-path)
      shift
      PA_WSGI_PATH="$1"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
  shift
done

activate_virtualenv() {
  local workon_home
  local direct_activate
  local had_nounset=0

  if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    log "Using existing virtualenv: ${VIRTUAL_ENV}"
    return
  fi

  workon_home="${WORKON_HOME:-$HOME/.virtualenvs}"
  direct_activate="${workon_home}/${PA_VENV_NAME}/bin/activate"

  if [[ -f "${direct_activate}" ]]; then
    # shellcheck source=/dev/null
    source "${direct_activate}"
    log "Activated virtualenv directly: ${PA_VENV_NAME}"
    return
  fi

  if [[ -f "/usr/local/bin/virtualenvwrapper.sh" ]]; then
    export WORKON_HOME="${workon_home}"
    export VIRTUALENVWRAPPER_PYTHON="${VIRTUALENVWRAPPER_PYTHON:-/usr/bin/python3}"

    case "$-" in
      *u*)
        had_nounset=1
        set +u
        ;;
    esac

    # shellcheck source=/dev/null
    source /usr/local/bin/virtualenvwrapper.sh
    workon "${PA_VENV_NAME}"

    if [[ "${had_nounset}" -eq 1 ]]; then
      set -u
    fi

    log "Activated virtualenv via virtualenvwrapper: ${PA_VENV_NAME}"
    return
  fi

  echo "Could not activate virtualenv '${PA_VENV_NAME}'." >&2
  echo "Create it first or pass a different name with --venv." >&2
  exit 1
}

run_step() {
  local label="$1"
  shift
  log "${label}"
  "$@"
}

log "Starting PeerTrain PythonAnywhere update"
log "Project directory: ${PROJECT_DIR}"
log "Git source: ${PA_REMOTE}/${PA_BRANCH}"
log "WSGI path: ${PA_WSGI_PATH}"

cd "${PROJECT_DIR}"
activate_virtualenv
run_step "Python version" python --version

if [[ -d ".git" ]]; then
  run_step "Pulling latest code" git pull --ff-only "${PA_REMOTE}" "${PA_BRANCH}"
else
  log "Skipping git pull because this directory is not a Git checkout."
fi

if [[ "${PA_INSTALL_DEPS}" -eq 1 ]]; then
  run_step "Installing dependencies" pip install -r requirements.txt
else
  log "Skipping dependency installation."
fi

if [[ "${PA_RUN_CHECK}" -eq 1 ]]; then
  run_step "Running Django checks" python manage.py check
else
  log "Skipping Django checks."
fi

if [[ "${PA_RUN_MIGRATE}" -eq 1 ]]; then
  run_step "Applying migrations" python manage.py migrate
else
  log "Skipping migrations."
fi

if [[ "${PA_RUN_STATIC}" -eq 1 ]]; then
  run_step "Collecting static files" python manage.py collectstatic --noinput
else
  log "Skipping collectstatic."
fi

if [[ "${PA_RUN_SEED}" -eq 1 ]]; then
  run_step "Refreshing demo seed data" python manage.py seed_peertrain
else
  log "Skipping seed_peertrain."
fi

if [[ "${PA_RUN_RELOAD}" -eq 1 ]]; then
  if [[ -f "${PA_WSGI_PATH}" ]]; then
    run_step "Reloading the web app" touch "${PA_WSGI_PATH}"
  else
    log "WSGI file not found, so auto-reload was skipped. Reload manually in the Web tab or pass --wsgi-path."
  fi
else
  log "Skipping app reload."
fi

log "PeerTrain update complete"
