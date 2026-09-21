#!/bin/sh
set -eu

APP_DIR=${PTY_MIRROR_HOME:-"${XDG_DATA_HOME:-$HOME/.local/share}/pty-mirror"}
BIN_DIR=${PTY_MIRROR_BIN_DIR:-"$HOME/.local/bin"}
REPO_RAW_URL=${PTY_MIRROR_RAW_URL:-"https://raw.githubusercontent.com/xikijinise/pty-mirror/main"}
FILES="pm pty-mirror pty_mirror.py pty_mirror_bashrc"

say() {
    printf '%s\n' "$*"
}

fail() {
    printf '安装失败：%s\n' "$*" >&2
    exit 1
}

uninstall_pty_mirror() {
    if [ -L "$BIN_DIR/pm" ]; then
        link_target=$(readlink -f -- "$BIN_DIR/pm" 2>/dev/null || true)
        expected_target=$(readlink -f -- "$APP_DIR/pm" 2>/dev/null || true)
        if [ -n "$link_target" ] && [ "$link_target" = "$expected_target" ]; then
            rm -f -- "$BIN_DIR/pm"
        fi
    fi

    for file in $FILES; do
        rm -f -- "$APP_DIR/$file"
    done
    rmdir -- "$APP_DIR" 2>/dev/null || true
    say "pty-mirror 已卸载。"
}

case "${1-}" in
    --uninstall)
        uninstall_pty_mirror
        exit 0
        ;;
    "") ;;
    *) fail "未知参数：$1" ;;
esac

install_apt_packages() {
    if [ "$(id -u)" -eq 0 ]; then
        apt-get install -y "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo apt-get install -y "$@"
    else
        fail "缺少依赖 $*，并且系统没有 sudo。请先手动安装。"
    fi
}

if ! command -v python3 >/dev/null 2>&1; then
    command -v apt-get >/dev/null 2>&1 || fail "找不到 python3，请先安装 Python 3。"
    install_apt_packages python3
fi

if ! python3 -c 'import pyte' >/dev/null 2>&1; then
    command -v apt-get >/dev/null 2>&1 || fail "找不到 pyte，请先安装 Python 包 pyte。"
    say "正在安装依赖 python3-pyte……"
    install_apt_packages python3-pyte
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || pwd)
SOURCE_DIR=
TEMP_DIR=

if [ -f "$SCRIPT_DIR/pty_mirror.py" ] && [ -f "$SCRIPT_DIR/pm" ]; then
    SOURCE_DIR=$SCRIPT_DIR
else
    command -v curl >/dev/null 2>&1 || fail "远程安装需要 curl。"
    TEMP_DIR=$(mktemp -d)
    trap 'rm -rf -- "$TEMP_DIR"' EXIT HUP INT TERM
    SOURCE_DIR=$TEMP_DIR
    say "正在下载 pty-mirror……"
    for file in $FILES; do
        curl -fsSL "$REPO_RAW_URL/$file" -o "$SOURCE_DIR/$file" || fail "下载 $file 失败。"
    done
fi

mkdir -p -- "$APP_DIR" "$BIN_DIR"
install -m 755 "$SOURCE_DIR/pm" "$APP_DIR/pm"
install -m 755 "$SOURCE_DIR/pty-mirror" "$APP_DIR/pty-mirror"
install -m 755 "$SOURCE_DIR/pty_mirror.py" "$APP_DIR/pty_mirror.py"
install -m 644 "$SOURCE_DIR/pty_mirror_bashrc" "$APP_DIR/pty_mirror_bashrc"
ln -sfn -- "$APP_DIR/pm" "$BIN_DIR/pm"

"$BIN_DIR/pm" l >/dev/null

say ""
say "安装完成。运行 pm，然后输入数字选择功能。"
if ! command -v pm >/dev/null 2>&1; then
    say "当前终端尚未包含 $BIN_DIR，请先运行："
    say "  export PATH=\"$BIN_DIR:\$PATH\""
fi
