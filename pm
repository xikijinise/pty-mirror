#!/bin/sh
set -eu

SCRIPT_PATH=$0
if command -v readlink >/dev/null 2>&1; then
    RESOLVED_PATH=$(readlink -f -- "$SCRIPT_PATH" 2>/dev/null || true)
    if [ -n "$RESOLVED_PATH" ]; then
        SCRIPT_PATH=$RESOLVED_PATH
    fi
fi
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)

show_menu() {
    cat <<'EOF'

pty-mirror
  1) 启动被监控终端
  2) 实时只读查看
  3) 查看当前状态后退出
  4) 输出 JSON 事件
  5) 列出运行中的会话
  6) 显示完整帮助
  0) 退出
EOF
    printf '请输入数字 [0-6]: '
    IFS= read -r choice || exit 1
    printf '\n'
}

if [ "$#" -eq 0 ]; then
    show_menu
    case "$choice" in
        1) set -- host ;;
        2) set -- view ;;
        3) set -- view --once ;;
        4) set -- json ;;
        5) set -- list ;;
        6) set -- help ;;
        0) exit 0 ;;
        *)
            echo "无效选项：$choice" >&2
            exit 2
            ;;
    esac
fi

case "${1-}" in
    host|h)
        shift
        exec "$SCRIPT_DIR/pty-mirror" host --name main --cwd "$PWD" "$@"
        ;;
    view|v)
        shift
        exec "$SCRIPT_DIR/pty-mirror" view --name main "$@"
        ;;
    json|j)
        shift
        exec "$SCRIPT_DIR/pty-mirror" view --name main --json "$@"
        ;;
    list|l)
        exec "$SCRIPT_DIR/pty-mirror" list
        ;;
    help|-h|--help)
        exec "$SCRIPT_DIR/pty-mirror" --help
        ;;
    *)
        echo "用法：pm（显示菜单），或 pm h / pm v / pm j / pm l" >&2
        exit 2
        ;;
esac
