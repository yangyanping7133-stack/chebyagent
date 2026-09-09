package com.chebysight.chebyagent.android;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONObject;

final class AgentSurfaceState {
    static final String PERMISSION_MISSING = "permission_missing";
    static final String MODEL_MISSING = "model_missing";
    static final String READY = "ready";
    static final String PLANNING = "planning";
    static final String RUNNING = "running";
    static final String PAUSED = "paused";
    static final String COMPLETED = "completed";
    static final String FAILED = "failed";

    private static final String PREFS = "chebyagent_surface_state";
    private static final String KEY_PHASE = "phase";
    private static final String KEY_TASK = "task";
    private static final String KEY_MODE = "mode";
    private static final String KEY_STEP = "step";
    private static final String KEY_PROGRESS = "progress";
    private static final String KEY_NEXT_ACTION = "next_action";
    private static final String KEY_SUMMARY = "summary";
    private static final String KEY_RESULT = "result";
    private static final String KEY_UPDATED_AT = "updated_at";

    private AgentSurfaceState() {
    }

    static JSONObject update(Context context, JSONObject input) {
        String phase = normalizePhase(input.optString("phase", RUNNING));
        SharedPreferences.Editor editor = prefs(context).edit();
        editor.putString(KEY_PHASE, phase);
        putString(editor, KEY_TASK, input.optString("task", ""));
        putString(editor, KEY_MODE, input.optString("mode", ""));
        putString(editor, KEY_STEP, input.optString("step", ""));
        putString(editor, KEY_PROGRESS, input.optString("progress", ""));
        putString(editor, KEY_NEXT_ACTION, input.optString("next_action", ""));
        putString(editor, KEY_SUMMARY, input.optString("summary", ""));
        putString(editor, KEY_RESULT, input.optString("result", ""));
        editor.putLong(KEY_UPDATED_AT, System.currentTimeMillis());
        editor.apply();
        return snapshot(context, AccessibilityState.isServiceEnabled(context));
    }

    static JSONObject clear(Context context) {
        prefs(context).edit().clear().apply();
        return snapshot(context, AccessibilityState.isServiceEnabled(context));
    }

    static void setPhase(Context context, String phase, String summary, String result) {
        JSONObject input = JsonUtil.obj(
                "phase", phase,
                "summary", summary == null ? "" : summary,
                "result", result == null ? "" : result
        );
        update(context, input);
    }

    static DisplayState display(Context context, boolean accessibilityReady) {
        SharedPreferences prefs = prefs(context);
        String storedPhase = normalizePhase(prefs.getString(KEY_PHASE, ""));
        String phase = resolvePhase(storedPhase, accessibilityReady);
        String task = prefs.getString(KEY_TASK, "");
        String mode = prefs.getString(KEY_MODE, "");
        String step = prefs.getString(KEY_STEP, "");
        String progress = prefs.getString(KEY_PROGRESS, "");
        String nextAction = prefs.getString(KEY_NEXT_ACTION, "");
        String summary = prefs.getString(KEY_SUMMARY, "");
        String result = prefs.getString(KEY_RESULT, "");

        if (PERMISSION_MISSING.equals(phase)) {
            return displayState(phase, "权限模式", "开启无障碍", "", "", "开启权限", "需要权限", "开启无障碍后才能控制手机", "", task, true, false, false);
        }
        if (MODEL_MISSING.equals(phase)) {
            return displayState(phase, emptyFallback(mode, "配置模式"), emptyFallback(step, "连接 Codex"), emptyFallback(progress, "0/4"), nextAction, "登录 Codex", "需要登录", emptyFallback(summary, "请完成 ChatGPT 登录"), "", task, true, true, false);
        }
        if (PLANNING.equals(phase)) {
            return displayState(phase, emptyFallback(mode, "规划模式"), emptyFallback(step, "拆解任务"), emptyFallback(progress, "1/4"), emptyFallback(nextAction, "选择下一步"), "规划中", "正在规划", emptyFallback(summary, "正在拆解任务和选择下一步"), "", task, false, true, false);
        }
        if (RUNNING.equals(phase)) {
            return displayState(phase, emptyFallback(mode, "执行模式"), emptyFallback(step, "观察手机"), emptyFallback(progress, "2/4"), emptyFallback(nextAction, "验证当前页面"), "已启动", "正在执行", emptyFallback(summary, "观察、操作、验证会连续记录"), "", task, false, true, false);
        }
        if (PAUSED.equals(phase)) {
            return displayState(phase, emptyFallback(mode, "透传模式"), emptyFallback(step, "保持连接"), progress, nextAction, "继续", "已记录", emptyFallback(summary, "透传模式下操作仍可继续"), "", task, true, true, false);
        }
        if (COMPLETED.equals(phase)) {
            return displayState(phase, emptyFallback(mode, "结果模式"), emptyFallback(step, "整理证据"), emptyFallback(progress, "4/4"), nextAction, "完成", "已完成", emptyFallback(summary, "结果已保存到记录"), result, task, true, true, true);
        }
        if (FAILED.equals(phase)) {
            return displayState(phase, emptyFallback(mode, "恢复模式"), emptyFallback(step, "等待重试"), progress, nextAction, "重试", "运行失败", emptyFallback(summary, "执行未完成"), result, task, true, true, true);
        }
        return displayState(READY, emptyFallback(mode, "目标模式"), emptyFallback(step, "定义目标"), emptyFallback(progress, "0/4"), emptyFallback(nextAction, "输入任务"), "启动", "准备好了", "输入任务后启动", "", task, true, true, true);
    }

    static JSONObject snapshot(Context context, boolean accessibilityReady) {
        DisplayState state = display(context, accessibilityReady);
        SharedPreferences prefs = prefs(context);
        boolean ready = READY.equals(state.phase);
        return JsonUtil.obj(
                "ok", true,
                "phase", state.phase,
                "mode_label", state.modeLabel,
                "step_label", state.stepLabel,
                "progress_label", state.progressLabel,
                "next_action_label", state.nextActionLabel,
                "status_label", state.statusLabel,
                "detail_label", state.detailLabel,
                "button_label", state.buttonLabel,
                "button_enabled", state.buttonEnabled,
                "task", prefs.getString(KEY_TASK, ""),
                "summary", ready ? "" : prefs.getString(KEY_SUMMARY, ""),
                "result", ready ? "" : prefs.getString(KEY_RESULT, ""),
                "updated_at", prefs.getLong(KEY_UPDATED_AT, 0L)
        );
    }

    private static String resolvePhase(String storedPhase, boolean accessibilityReady) {
        if (!accessibilityReady && PERMISSION_MISSING.equals(storedPhase)) return PERMISSION_MISSING;
        if (!AgentNodeService.isRunning()
                && (PLANNING.equals(storedPhase)
                || RUNNING.equals(storedPhase)
                || PAUSED.equals(storedPhase))) {
            return READY;
        }
        if ((RUNNING.equals(storedPhase) || PAUSED.equals(storedPhase)) && !AgentNodeService.isRunning()) return READY;
        if (storedPhase.length() > 0 && !READY.equals(storedPhase)) {
            return storedPhase;
        }
        return READY;
    }

    private static String normalizePhase(String phase) {
        if (phase == null) return "";
        String normalized = phase.trim().toLowerCase(java.util.Locale.US).replace("-", "_");
        if (PERMISSION_MISSING.equals(normalized)
                || MODEL_MISSING.equals(normalized)
                || READY.equals(normalized)
                || PLANNING.equals(normalized)
                || RUNNING.equals(normalized)
                || PAUSED.equals(normalized)
                || COMPLETED.equals(normalized)
                || FAILED.equals(normalized)) {
            return normalized;
        }
        return "";
    }

    private static SharedPreferences prefs(Context context) {
        return context.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private static void putString(SharedPreferences.Editor editor, String key, String value) {
        if (value == null || value.trim().isEmpty()) {
            editor.remove(key);
        } else {
            editor.putString(key, value.trim());
        }
    }

    private static String emptyFallback(String value, String fallback) {
        return value == null || value.trim().isEmpty() ? fallback : value.trim();
    }

    private static DisplayState displayState(
            String phase,
            String modeLabel,
            String stepLabel,
            String progressLabel,
            String nextActionLabel,
            String buttonLabel,
            String statusLabel,
            String detailLabel,
            String resultLabel,
            String task,
            boolean buttonEnabled,
            boolean taskVisible,
            boolean historyVisible
    ) {
        return new DisplayState(
                phase,
                modeLabel,
                stepLabel,
                progressLabel,
                nextActionLabel,
                buttonLabel,
                statusLabel,
                detailLabel,
                resultLabel,
                task,
                buttonEnabled,
                taskVisible,
                historyVisible
        );
    }

    static final class DisplayState {
        final String phase;
        final String modeLabel;
        final String stepLabel;
        final String progressLabel;
        final String nextActionLabel;
        final String buttonLabel;
        final String statusLabel;
        final String detailLabel;
        final String resultLabel;
        final String task;
        final boolean buttonEnabled;
        final boolean taskVisible;
        final boolean historyVisible;

        DisplayState(
                String phase,
                String modeLabel,
                String stepLabel,
                String progressLabel,
                String nextActionLabel,
                String buttonLabel,
                String statusLabel,
                String detailLabel,
                String resultLabel,
                String task,
                boolean buttonEnabled,
                boolean taskVisible,
                boolean historyVisible
        ) {
            this.phase = phase;
            this.modeLabel = modeLabel;
            this.stepLabel = stepLabel;
            this.progressLabel = progressLabel;
            this.nextActionLabel = nextActionLabel;
            this.buttonLabel = buttonLabel;
            this.statusLabel = statusLabel;
            this.detailLabel = detailLabel;
            this.resultLabel = resultLabel;
            this.task = task;
            this.buttonEnabled = buttonEnabled;
            this.taskVisible = taskVisible;
            this.historyVisible = historyVisible;
        }
    }
}
