#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

install_packages() {
    if ! command -v apt-get >/dev/null; then
        printf 'Install these packages with your Linux package manager: %s\n' "$*" >&2
        return 1
    fi
    local admin=()
    if (( EUID != 0 )); then
        admin=(sudo)
    fi
    "${admin[@]}" apt-get update
    "${admin[@]}" apt-get install -y "$@"
}

packages=()
command -v git >/dev/null || packages+=(git)
command -v python3 >/dev/null || packages+=(python3 python3-venv)
if command -v apt-get >/dev/null && ! dpkg-query -W -f='${Status}\n' libxcb-cursor0 2>/dev/null | grep -qx 'install ok installed'; then
    packages+=(libxcb-cursor0)
fi
if (( ${#packages[@]} )); then
    install_packages "${packages[@]}"
fi

venv="${ORCHESTRA_VENV:-$HOME/.venvs/orchestra}"
python="$venv/bin/python"
if ! "$python" -m pip --version >/dev/null 2>&1; then
    if ! python3 -m venv "$venv"; then
        install_packages python3-venv
        python3 -m venv "$venv"
    fi
fi

requirements=$(sha256sum requirements.txt requirements-qt.txt)
stamp="$venv/.orchestra-requirements"
if [[ ! -f "$stamp" || $(<"$stamp") != "$requirements" ]] ||
    ! "$python" -B -c 'import PySide6.QtWidgets, openai, regex, rich' >/dev/null 2>&1; then
    "$python" -m pip install -r requirements-qt.txt
    printf '%s\n' "$requirements" > "$stamp"
fi

export VIRTUAL_ENV="$venv"
export PATH="$venv/bin:$PATH"
exec "$python" -B orchestra_qt.py "$@"
