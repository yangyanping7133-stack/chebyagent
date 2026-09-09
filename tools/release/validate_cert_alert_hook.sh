#!/bin/sh
set -eu

hook=${1:-/etc/chebycodex/cert-alert-hook}

[ -e "$hook" ] || {
    echo "BLOCKED: external certificate alert hook is missing: $hook" >&2
    exit 1
}
[ ! -L "$hook" ] || {
    echo "BLOCKED: certificate alert hook must not be a symlink" >&2
    exit 1
}
[ -f "$hook" ] || {
    echo "BLOCKED: certificate alert hook must be a regular file" >&2
    exit 1
}
[ -x "$hook" ] || {
    echo "BLOCKED: certificate alert hook must be executable" >&2
    exit 1
}

hook_uid=$(stat -c '%u' "$hook")
[ "$hook_uid" = "0" ] || {
    echo "BLOCKED: certificate alert hook must be owned by root" >&2
    exit 1
}
hook_permissions=$(stat -c '%A' "$hook")
case "$hook_permissions" in
    ?????w????|????????w?)
        echo "BLOCKED: certificate alert hook must not be group/world-writable" >&2
        exit 1
        ;;
    *) ;;
esac

exit 0
