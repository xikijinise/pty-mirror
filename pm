#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

case "${1-}" in
    ""|host|h)
        if [ "${1-}" = "host" ] || [ "${1-}" = "h" ]; then
            shift
        fi
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
        echo "用法：pm（启动被监控终端）或 pm v（只读查看）" >&2
        exit 2
        ;;
esac
