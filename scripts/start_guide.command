#!/bin/sh
# Tongle interactive guide launcher.
# Runs the sibling tongle executable with the "guide" command and forwards
# all arguments. Keeps its own directory, the exit status and a friendly
# Chinese failure message. Does not depend on the user shell, Python or pi.

# Resolve this script's directory safely, without relying on $0 being a path.
self=$0
while [ -L "$self" ]; do
    dir=$(cd -P -- "$(dirname -- "$self")" && pwd)
    link=$(readlink -- "$self")
    case $link in
        /*) self=$link ;;
        *) self=$dir/$link ;;
    esac
done
here=$(cd -P -- "$(dirname -- "$self")" && pwd)

exe="$here/tongle"
if [ ! -x "$exe" ]; then
    echo "未找到可执行程序：$exe" >&2
    echo "请确认已完整解压发布包，或在项目目录运行构建脚本重新生成。" >&2
    exit 1
fi

"$exe" guide "$@"
status=$?

# Pause only on a real terminal so piped/automated runs never hang.
if [ -t 0 ]; then
    printf '按回车键退出…'
    read -r _dummy
fi

exit $status
