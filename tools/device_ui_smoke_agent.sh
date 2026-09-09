#!/system/bin/sh
# Runs the release UI gate entirely on the phone. The host supplies no coordinates.

set -eu

PACKAGE=com.cheby.codex.mobile
LOCK_DIR=/data/local/tmp/chebycodex-ui-agent.lock
LOCK_OWNER=$LOCK_DIR/owner
MAX_ACTIONS=4
MAX_POLLS=${3:-120}
PROMPT_ENCODED=${1:-}
EXPECTED_ANSWER=${2:-}
RUN_TOKEN=${4:-}
poll=0
actions=0

if ! printf '%s' "$PROMPT_ENCODED" | grep -Eq '^[A-Za-z0-9]+(%s[A-Za-z0-9]+)*$' \
    || ! printf '%s' "$EXPECTED_ANSWER" | grep -Eq '^[A-Za-z0-9_]+$' \
    || ! printf '%s' "$RUN_TOKEN" | grep -Eq '^[A-Za-z0-9]+$'; then
    echo "ui_agent=invalid_input" >&2
    exit 2
fi
PROMPT=$(printf '%s' "$PROMPT_ENCODED" | sed 's/%s/ /g')
RUN_PREFIX=/data/local/tmp/chebycodex-ui-agent-$RUN_TOKEN
UI_DUMP=$RUN_PREFIX.xml
FINAL_XML=$RUN_PREFIX-final.xml
CONNECTED_PNG=$RUN_PREFIX-connected.png
FINAL_PNG=$RUN_PREFIX-final.png
BASELINE_TEXT=$RUN_PREFIX-baseline.txt
CURRENT_TEXT=$RUN_PREFIX-current.txt
PID_FILE=$RUN_PREFIX.pid

display_size=$(wm size 2>/dev/null | sed -n 's/.*: \([0-9][0-9]*\)x\([0-9][0-9]*\).*/\1 \2/p' | tail -n 1)
set -- $display_size
if [ "$#" -ne 2 ]; then
    echo "ui_agent=unknown_display_size" >&2
    exit 2
fi
display_width=$1
display_height=$2

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "ui_agent=already_running" >&2
    exit 5
fi
printf '%s\n' "$RUN_TOKEN" >"$LOCK_OWNER"
trap 'rm -f "$UI_DUMP" "$BASELINE_TEXT" "$CURRENT_TEXT" "$PID_FILE" "$LOCK_OWNER"; rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM
echo $$ >"$PID_FILE"

cleanup() {
    rm -f "$UI_DUMP" "$BASELINE_TEXT" "$CURRENT_TEXT" "$PID_FILE" "$LOCK_OWNER"
    rmdir "$LOCK_DIR" 2>/dev/null || true
}
capture_failure() {
    if [ -f "$UI_DUMP" ]; then
        cp "$UI_DUMP" "$FINAL_XML"
    fi
    screencap -p >"$FINAL_PNG" 2>/dev/null || true
}
trap cleanup EXIT
trap 'capture_failure; cleanup; exit 143' TERM
trap 'capture_failure; cleanup; exit 130' INT

dump_ui() {
    rm -f "$UI_DUMP"
    uiautomator dump "$UI_DUMP" >/dev/null 2>&1 || return 1
    [ -s "$UI_DUMP" ] || return 1
    grep -Fq "package=\"$PACKAGE\"" "$UI_DUMP" || return 1
    focused_window=$(dumpsys window 2>/dev/null | grep -m 1 'mCurrentFocus=' || true)
    printf '%s\n' "$focused_window" | grep -Fq " u0 $PACKAGE/"
}

write_dynamic_texts() {
    destination=$1
    sed 's/></>\
</g' "$UI_DUMP" \
        | grep -F "package=\"$PACKAGE\"" \
        | sed -n 's/.*text="\([^"]*\)".*/\1/p' \
        | grep -Ev '^(今天|C|ChebyAgent|ChebyCodex|服务已连接|新会话|发送消息|Updates|Codex completed the task|Codex is working|已送达|已排队|未发送|暂时离线|[0-9]+ activity updates?)$' \
        | grep -v '^$' \
        | sort -u >"$destination" || true
}

node_for() {
    node_pattern=$1
    sed 's/></>\
</g' "$UI_DUMP" \
        | grep -F "package=\"$PACKAGE\"" \
        | grep -F "$node_pattern" \
        | grep 'clickable="true"' \
        | grep 'enabled="true"' \
        | head -n 1 || true
}

tap_selector() {
    selector=$1
    capture_baseline=${2:-no}
    required_text=${3:-}
    expected_composer=${4:-}
    require_ready=${5:-no}
    if ! dump_ui; then
        return 1
    fi
    if [ -n "$required_text" ] && ! sed 's/></>\
</g' "$UI_DUMP" \
        | grep -F "package=\"$PACKAGE\"" \
        | grep -F "text=\"$required_text\"" >/dev/null; then
        return 1
    fi
    package_nodes=$(sed 's/></>\
</g' "$UI_DUMP" | grep -F "package=\"$PACKAGE\"" || true)
    if [ "$require_ready" = yes ]; then
        if printf '%s\n' "$package_nodes" \
            | grep -Eq 'text="(正在安全恢复会话|同步完成前暂停发送)"'; then
            return 1
        fi
        ready_composer=$(printf '%s\n' "$package_nodes" \
            | grep -F 'class="android.widget.EditText"' \
            | grep -F 'enabled="true"' || true)
        [ -n "$ready_composer" ] || return 1
    fi
    if [ -n "$expected_composer" ]; then
        composer_nodes=$(printf '%s\n' "$package_nodes" \
            | grep -F 'class="android.widget.EditText"' || true)
        [ "$(printf '%s\n' "$composer_nodes" | grep -c .)" -eq 1 ] \
            && printf '%s\n' "$composer_nodes" \
                | grep -F "text=\"$expected_composer\"" >/dev/null \
            || return 1
    fi
    target=$(node_for "$selector")
    if [ -z "$target" ]; then
        return 1
    fi
    if [ "$capture_baseline" = yes ]; then
        write_dynamic_texts "$BASELINE_TEXT"
    fi
    bounds=$(printf '%s\n' "$target" \
        | sed -n 's/.*bounds="\[\([0-9][0-9]*\),\([0-9][0-9]*\)\]\[\([0-9][0-9]*\),\([0-9][0-9]*\)\]".*/\1 \2 \3 \4/p')
    set -- $bounds
    if [ "$#" -ne 4 ] || [ "$3" -le "$1" ] || [ "$4" -le "$2" ] \
        || [ "$3" -gt "$display_width" ] || [ "$4" -gt "$display_height" ]; then
        echo "ui_agent=invalid_bounds" >&2
        capture_failure
        exit 3
    fi
    if [ "$actions" -ge "$MAX_ACTIONS" ]; then
        echo "ui_agent=action_limit" >&2
        capture_failure
        exit 3
    fi
    input tap $((($1 + $3) / 2)) $((($2 + $4) / 2))
    actions=$((actions + 1))
}

wait_and_tap() {
    selector=$1
    capture_baseline=${2:-no}
    required_text=${3:-}
    expected_composer=${4:-}
    require_ready=${5:-no}
    limit=${6:-30}
    attempt=0
    while [ "$attempt" -lt "$limit" ]; do
        attempt=$((attempt + 1))
        if tap_selector "$selector" "$capture_baseline" "$required_text" "$expected_composer" "$require_ready"; then
            return 0
        fi
        sleep 1
    done
    echo "ui_agent=action_target_timeout" >&2
    capture_failure
    exit 4
}

input_prompt() {
    if ! dump_ui; then
        return 1
    fi
    target=$(node_for 'class="android.widget.EditText"')
    if [ -z "$target" ] \
        || ! sed 's/></>\
</g' "$UI_DUMP" | grep -F "package=\"$PACKAGE\"" | grep -F 'text="服务已连接"' >/dev/null \
        || grep -Eq 'text="(正在安全恢复会话|同步完成前暂停发送)"' "$UI_DUMP" \
        || ! printf '%s\n' "$target" | grep -Fq 'focused="true"' \
        || ! printf '%s\n' "$target" | grep -Fq 'text=""'; then
        return 1
    fi
    if [ "$actions" -ge "$MAX_ACTIONS" ]; then
        echo "ui_agent=action_limit" >&2
        capture_failure
        exit 3
    fi
    input text "$PROMPT_ENCODED"
    actions=$((actions + 1))
}

wait_and_input() {
    attempt=0
    while [ "$attempt" -lt 10 ]; do
        attempt=$((attempt + 1))
        if input_prompt; then
            return 0
        fi
        sleep 1
    done
    echo "ui_agent=input_target_timeout" >&2
    capture_failure
    exit 4
}

wait_for_text() {
    expected=$1
    limit=$2
    count=0
    while [ "$count" -lt "$limit" ]; do
        count=$((count + 1))
        if dump_ui && sed 's/></>\
</g' "$UI_DUMP" \
            | grep -F "package=\"$PACKAGE\"" \
            | grep -F "text=\"$expected\"" >/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}

rm -f "$FINAL_XML" "$CONNECTED_PNG" "$FINAL_PNG"
monkey -p "$PACKAGE" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1

connection_streak=0
poll=0
while [ "$poll" -lt 60 ] && [ "$connection_streak" -lt 3 ]; do
    poll=$((poll + 1))
    if dump_ui && sed 's/></>\
</g' "$UI_DUMP" \
        | grep -F "package=\"$PACKAGE\"" \
        | grep -F 'text="服务已连接"' >/dev/null \
        && [ -n "$(node_for 'resource-id="new_thread"')" ] \
        && [ -n "$(node_for 'class="android.widget.EditText"')" ] \
        && ! grep -Eq 'text="(正在安全恢复会话|同步完成前暂停发送)"' "$UI_DUMP"; then
        connection_streak=$((connection_streak + 1))
    else
        connection_streak=0
    fi
    [ "$connection_streak" -ge 3 ] || sleep 1
done
if [ "$connection_streak" -lt 3 ]; then
    echo "ui_agent=connection_timeout" >&2
    capture_failure
    exit 4
fi
screencap -p >"$CONNECTED_PNG"
wait_and_tap 'resource-id="new_thread"' yes '服务已连接' '' yes 30

composer=
poll=0
while [ "$poll" -lt 30 ]; do
    poll=$((poll + 1))
    if dump_ui \
        && ! grep -Eq 'text="(Codex completed the task|Codex is working|已送达|已排队|未发送)"' "$UI_DUMP"; then
        composer=$(node_for 'class="android.widget.EditText"')
        if [ -n "$composer" ] && printf '%s\n' "$composer" | grep -q 'text=""'; then
            write_dynamic_texts "$CURRENT_TEXT"
            if [ ! -s "$BASELINE_TEXT" ] \
                || ! grep -Fxf "$BASELINE_TEXT" "$CURRENT_TEXT" >/dev/null; then
                break
            fi
        fi
    fi
    composer=
    sleep 1
done
if [ -z "$composer" ]; then
    echo "ui_agent=empty_composer_timeout" >&2
    capture_failure
    exit 4
fi
wait_and_tap 'class="android.widget.EditText"' no '服务已连接' '' yes 30

poll=0
while [ "$poll" -lt 10 ]; do
    poll=$((poll + 1))
    if dump_ui; then
        composer=$(node_for 'class="android.widget.EditText"')
        if [ -n "$composer" ] && printf '%s\n' "$composer" | grep -q 'focused="true"'; then
            break
        fi
    fi
    composer=
    sleep 1
done
if [ -z "$composer" ]; then
    echo "ui_agent=composer_focus_timeout" >&2
    capture_failure
    exit 4
fi

wait_and_input
if ! wait_for_text "$PROMPT" 10; then
    echo "ui_agent=input_timeout" >&2
    capture_failure
    exit 4
fi
wait_and_tap 'resource-id="send_message"' no '服务已连接' "$PROMPT" yes 15

poll=0
while [ "$poll" -lt "$MAX_POLLS" ]; do
    poll=$((poll + 1))
    if dump_ui; then
        package_nodes=$(sed 's/></>\
</g' "$UI_DUMP" | grep -F "package=\"$PACKAGE\"" || true)
        if printf '%s\n' "$package_nodes" | grep -F 'resource-id="codex_answer"' >/dev/null \
            && printf '%s\n' "$package_nodes" | grep -F 'text="Codex completed the task"' >/dev/null \
            && ! printf '%s\n' "$package_nodes" \
                | grep -Eq 'text="(发送失败|状态待同步|未发送|绑定失败|已排队|暂时离线)"'; then
            cp "$UI_DUMP" "$FINAL_XML"
            screencap -p >"$FINAL_PNG"
            echo "ui_agent=evidence_ready actions=$actions polls=$poll"
            exit 0
        fi
    fi
    sleep 1
done

echo "ui_agent=answer_timeout actions=$actions polls=$poll" >&2
capture_failure
exit 4
