#!/system/bin/sh
# Phone-side, fail-closed Huawei package-install agent. It starts and owns the exact install.

set -eu

PACKAGE_INSTALLER=com.android.packageinstaller
UI_DUMP=/data/local/tmp/chebycodex-installer-agent.xml
LOCK_DIR=/data/local/tmp/chebycodex-installer-agent.lock
MAX_POLLS=120
MAX_ACTIONS=6
EXPECTED_APK_PATH=${1:-}
EXPECTED_APK_SHA256=${2:-}
RESULT_FILE=/data/local/tmp/chebycodex-installer-agent.result
INSTALL_PID=
poll=0
actions=0
away_polls=0
saw_install_action=0
clicked_done=0

cleanup() {
    rm -f "$UI_DUMP" "$RESULT_FILE"
    if [ -n "$INSTALL_PID" ] && [ -r "/proc/$INSTALL_PID/cmdline" ]; then
        cleanup_arguments=$(tr '\000' '\n' <"/proc/$INSTALL_PID/cmdline" 2>/dev/null || true)
        set -- $cleanup_arguments
        if [ "$#" -eq 5 ] && [ "${1##*/}" = cmd ] && [ "$2" = package ] \
            && [ "$3" = install ] && [ "$4" = -r ] && [ "$5" = "$EXPECTED_APK_PATH" ]; then
            kill -TERM "$INSTALL_PID" 2>/dev/null || true
            wait "$INSTALL_PID" 2>/dev/null || true
        fi
    fi
    rmdir "$LOCK_DIR" 2>/dev/null || true
}

if ! printf '%s' "$EXPECTED_APK_PATH" | grep -Eq '^/data/local/tmp/[A-Za-z0-9._-]+\.apk$' \
    || ! printf '%s' "$EXPECTED_APK_SHA256" | grep -Eq '^[0-9a-f]{64}$'; then
    echo "installer_agent=invalid_target" >&2
    exit 2
fi

display_size=$(wm size 2>/dev/null | sed -n 's/.*: \([0-9][0-9]*\)x\([0-9][0-9]*\).*/\1 \2/p' | tail -n 1)
set -- $display_size
if [ "$#" -ne 2 ]; then
    echo "installer_agent=unknown_display_size" >&2
    exit 2
fi
display_width=$1
display_height=$2

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "installer_agent=already_running" >&2
    exit 5
fi
trap cleanup EXIT INT TERM

validate_staged_apk() {
    [ -f "$EXPECTED_APK_PATH" ] \
        && [ ! -L "$EXPECTED_APK_PATH" ] \
        && [ "$(sha256sum "$EXPECTED_APK_PATH" 2>/dev/null | awk '{print $1}')" = "$EXPECTED_APK_SHA256" ]
}

scan_install_transactions() {
    transaction_count=0
    target_transaction_count=0
    own_target_seen=0
    for process_dir in /proc/[0-9]*; do
        process_name=
        IFS= read -r process_name <"$process_dir/comm" 2>/dev/null || continue
        case "$process_name" in
            sh|cmd|pm) ;;
            *) continue ;;
        esac
        [ -r "$process_dir/cmdline" ] || continue
        arguments=$(tr '\000' '\n' <"$process_dir/cmdline" 2>/dev/null || true)
        [ -n "$arguments" ] || continue
        set -- $arguments
        is_install=0
        target_path=
        executable=${1##*/}
        if [ "$executable" = sh ] && [ "${2:-}" = /system/bin/pm ]; then
            shift 2
            for argument in "$@"; do
                case "$argument" in install*) is_install=1 ;; esac
            done
        elif [ "$executable" = pm ]; then
            shift 1
            for argument in "$@"; do
                case "$argument" in install*) is_install=1 ;; esac
            done
        elif [ "$executable" = cmd ] && [ "${2:-}" = package ]; then
            shift 2
            for argument in "$@"; do
                case "$argument" in install*) is_install=1 ;; esac
            done
        fi
        [ "$is_install" -eq 1 ] || continue
        transaction_count=$((transaction_count + 1))
        raw_arguments=$(tr '\000' '\n' <"$process_dir/cmdline" 2>/dev/null || true)
        set -- $raw_arguments
        if [ "$#" -eq 5 ] && [ "${1##*/}" = cmd ] && [ "$2" = package ] \
            && [ "$3" = install ] && [ "$4" = -r ] && [ "$5" = "$EXPECTED_APK_PATH" ]; then
            target_path=$5
            target_transaction_count=$((target_transaction_count + 1))
        fi
        if [ -n "$INSTALL_PID" ] && [ "${process_dir##*/}" = "$INSTALL_PID" ] \
            && [ "$target_path" = "$EXPECTED_APK_PATH" ]; then
            own_target_seen=1
        fi
    done
}

validate_live_target_transaction() {
    validate_staged_apk || return 1
    scan_install_transactions
    [ "$own_target_seen" -eq 1 ] \
        && [ "$transaction_count" -eq 1 ] \
        && [ "$target_transaction_count" -eq 1 ]
}

dump_installer_ui() {
    rm -f "$UI_DUMP"
    uiautomator dump "$UI_DUMP" >/dev/null 2>&1 || return 1
    [ -s "$UI_DUMP" ] || return 1
    grep -Fq "package=\"$PACKAGE_INSTALLER\"" "$UI_DUMP" || return 1
    focused_window=$(dumpsys window 2>/dev/null | grep -m 1 'mCurrentFocus=' || true)
    printf '%s\n' "$focused_window" | grep -Fq " u0 $PACKAGE_INSTALLER/"
}

node_for() {
    action_kind=$1
    candidates=$(sed 's/></>\
</g' "$UI_DUMP" \
        | grep -F "package=\"$PACKAGE_INSTALLER\"" \
        | grep -F 'class="android.widget.Button"' \
        | grep -F 'clickable="true"' \
        | grep -F 'enabled="true"' || true)
    if [ "$action_kind" = install ]; then
        printf '%s\n' "$candidates" \
            | grep -F 'resource-id="android:id/button1"' \
            | grep -E 'text="(继续安装|安装|Install anyway|Install)"' \
            | head -n 1 || true
    else
        printf '%s\n' "$candidates" \
            | grep -E 'resource-id="(android:id/button(1|2)|com.android.packageinstaller:id/done_button)"' \
            | grep -E 'text="(完成|Done)"' \
            | head -n 1 || true
    fi
}

tap_fresh_target() {
    action_kind=$1
    if ! dump_installer_ui; then
        echo "installer_agent=focus_lost" >&2
        exit 3
    fi
    if [ "$action_kind" = install ]; then
        validate_live_target_transaction || {
            echo "installer_agent=transaction_lost" >&2
            exit 3
        }
    else
        [ "$saw_install_action" -eq 1 ] \
            && [ "$(tr -d '\r\n' <"$RESULT_FILE" 2>/dev/null || true)" = Success ] \
            || {
                echo "installer_agent=install_not_successful" >&2
                exit 3
            }
        scan_install_transactions
        [ "$transaction_count" -eq 0 ] || {
            echo "installer_agent=concurrent_transaction" >&2
            exit 3
        }
    fi
    target=$(node_for "$action_kind")
    [ -n "$target" ] || {
        echo "installer_agent=target_missing" >&2
        exit 3
    }
    bounds=$(printf '%s\n' "$target" \
        | sed -n 's/.*bounds="\[\([0-9][0-9]*\),\([0-9][0-9]*\)\]\[\([0-9][0-9]*\),\([0-9][0-9]*\)\]".*/\1 \2 \3 \4/p')
    set -- $bounds
    if [ "$#" -ne 4 ] || [ "$3" -le "$1" ] || [ "$4" -le "$2" ] \
        || [ "$3" -gt "$display_width" ] || [ "$4" -gt "$display_height" ]; then
        echo "installer_agent=invalid_bounds" >&2
        exit 3
    fi
    if [ "$actions" -ge "$MAX_ACTIONS" ]; then
        echo "installer_agent=action_limit" >&2
        exit 3
    fi
    input tap $((($1 + $3) / 2)) $((($2 + $4) / 2))
    actions=$((actions + 1))
    echo "installer_agent=action kind=$action_kind actions=$actions"
}

validate_staged_apk || {
    echo "installer_agent=apk_hash_mismatch" >&2
    exit 2
}
scan_install_transactions
[ "$transaction_count" -eq 0 ] || {
    echo "installer_agent=preexisting_transaction" >&2
    exit 3
}
am force-stop "$PACKAGE_INSTALLER" >/dev/null 2>&1 || true
rm -f "$RESULT_FILE"
/system/bin/cmd package install -r "$EXPECTED_APK_PATH" >"$RESULT_FILE" 2>&1 &
INSTALL_PID=$!

while [ "$poll" -lt "$MAX_POLLS" ]; do
    poll=$((poll + 1))
    if ! dump_installer_ui; then
        away_polls=$((away_polls + 1))
        if [ "$clicked_done" -eq 1 ] && [ "$away_polls" -ge 3 ] \
            && [ "$(tr -d '\r\n' <"$RESULT_FILE" 2>/dev/null || true)" = Success ]; then
            echo "installer_agent=completed actions=$actions"
            exit 0
        fi
        sleep 1
        continue
    fi
    away_polls=0

    if [ "$clicked_done" -eq 1 ]; then
        sleep 1
        continue
    fi

    install_target=$(node_for install)
    if [ -n "$install_target" ]; then
        tap_fresh_target install
        saw_install_action=1
        sleep 1
        continue
    fi

    if [ "$saw_install_action" -eq 1 ]; then
        done_target=$(node_for done)
        if [ -n "$done_target" ]; then
            tap_fresh_target done
            clicked_done=1
        fi
    fi
    sleep 1
done

echo "installer_agent=timeout install_action=$saw_install_action done=$clicked_done actions=$actions" >&2
exit 4
