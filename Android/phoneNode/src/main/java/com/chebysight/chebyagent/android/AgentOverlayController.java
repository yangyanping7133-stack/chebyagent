package com.chebysight.chebyagent.android;

import android.content.Intent;
import android.util.DisplayMetrics;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONObject;

final class AgentOverlayController {
    private static final AgentOverlayController INSTANCE = new AgentOverlayController();

    private static final int INK = Color.rgb(28, 28, 30);
    private static final int MUTED = Color.rgb(110, 110, 115);
    private static final int GREEN = Color.rgb(52, 199, 89);
    private static final int BLUE = Color.rgb(10, 132, 255);
    private static final int ORANGE = Color.rgb(255, 149, 0);
    private static final int RED = Color.rgb(255, 59, 48);
    private static final int WHITE = Color.WHITE;
    private static final long COMPACT_DELAY_MS = 4200L;
    private static final long POSITION_INTERVAL_MS = 600L;
    private static final int NORMAL_BOTTOM_OFFSET_DP = 22;
    private static final int KEYBOARD_BOTTOM_OFFSET_DP = 340;

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private AgentAccessibilityService service;
    private WindowManager windowManager;
    private LinearLayout root;
    private TextView statusDot;
    private TextView title;
    private TextView subtitle;
    private TextView inputAction;
    private TextView voiceAction;
    private TextView soundAction;
    private TextView trailing;
    private TextView stopAction;
    private WindowManager.LayoutParams params;
    private boolean manuallyPositioned = false;
    private boolean dragging = false;
    private float downRawX = 0f;
    private float downRawY = 0f;
    private int downX = 0;
    private int downY = 0;
    private String visibleMode = "";
    private String visibleState = "";
    private boolean visiblePaused = false;

    private final Runnable compactRunnable = () -> showCompact("已启动");
    private final Runnable positionRunnable = new Runnable() {
        @Override
        public void run() {
            updateWindowPosition();
            if (root != null) {
                mainHandler.postDelayed(this, POSITION_INTERVAL_MS);
            }
        }
    };

    static AgentOverlayController get() {
        return INSTANCE;
    }

    boolean isVisible() {
        return root != null;
    }

    void attach(AgentAccessibilityService service) {
        mainHandler.post(() -> {
            this.service = service;
            this.windowManager = (WindowManager) service.getSystemService(android.content.Context.WINDOW_SERVICE);
            if (windowManager == null || root != null) {
                return;
            }
            buildView(service);
            params = new WindowManager.LayoutParams(
                    WindowManager.LayoutParams.WRAP_CONTENT,
                    WindowManager.LayoutParams.WRAP_CONTENT,
                    WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
                    WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                            | WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL
                            | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                    PixelFormat.TRANSLUCENT
            );
            params.gravity = Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL;
            params.y = dp(NORMAL_BOTTOM_OFFSET_DP);
            try {
                windowManager.addView(root, params);
                schedulePositionUpdates();
            } catch (Exception ignored) {
                root = null;
            }
        });
    }

    void detach() {
        mainHandler.post(() -> {
            mainHandler.removeCallbacks(compactRunnable);
            mainHandler.removeCallbacks(positionRunnable);
            removeRoot(true);
        });
    }

    void showCompact(String state) {
        mainHandler.post(() -> {
            if (root == null) {
                if (service != null) attach(service);
                return;
            }
            boolean paused = false;
            String nextState = state == null ? "" : state;
            if ("compact".equals(visibleMode)
                    && nextState.equals(visibleState)
                    && paused == visiblePaused) {
                updateWindowPosition();
                return;
            }
            mainHandler.removeCallbacks(compactRunnable);
            root.setOrientation(LinearLayout.HORIZONTAL);
            root.setGravity(Gravity.CENTER_VERTICAL);
            root.setPadding(dp(12), dp(8), dp(8), dp(8));
            root.setBackground(rounded(WHITE, dp(22), Color.argb(30, 0, 0, 0), dp(1)));
            root.setMinimumWidth(dp(346));
            statusDot.setText("●");
            statusDot.setTextColor(GREEN);
            title.setText("Agent");
            title.setTextColor(INK);
            title.setTextSize(14);
            subtitle.setText(nextState);
            subtitle.setTextColor(MUTED);
            subtitle.setTextSize(11);
            inputAction.setVisibility(View.VISIBLE);
            voiceAction.setVisibility(View.VISIBLE);
            soundAction.setVisibility(View.VISIBLE);
            trailing.setVisibility(View.GONE);
            stopAction.setText("停止");
            stopAction.setTextColor(WHITE);
            stopAction.setBackground(rounded(RED, dp(14), Color.TRANSPARENT, 0));
            stopAction.setVisibility(View.VISIBLE);
            updateMargins(false, true);
            updateWindowPosition();
            schedulePositionUpdates();
            visibleMode = "compact";
            visibleState = nextState;
            visiblePaused = paused;
        });
    }

    void showRemote() {
        mainHandler.post(() -> {
            if (root == null) {
                if (service != null) attach(service);
                return;
            }
            boolean paused = false;
            if ("remote".equals(visibleMode) && paused == visiblePaused) {
                updateWindowPosition();
                return;
            }
            mainHandler.removeCallbacks(compactRunnable);
            root.setOrientation(LinearLayout.HORIZONTAL);
            root.setGravity(Gravity.CENTER_VERTICAL);
            root.setPadding(dp(12), dp(8), dp(8), dp(8));
            root.setMinimumWidth(dp(346));
            root.setBackground(rounded(WHITE, dp(22), Color.argb(34, 0, 0, 0), dp(1)));
            statusDot.setText("●");
            statusDot.setTextColor(GREEN);
            title.setText("Agent");
            title.setTextColor(INK);
            title.setTextSize(14);
            subtitle.setText("正在运行");
            subtitle.setTextColor(MUTED);
            subtitle.setTextSize(11);
            inputAction.setVisibility(View.VISIBLE);
            voiceAction.setVisibility(View.VISIBLE);
            soundAction.setVisibility(View.VISIBLE);
            trailing.setVisibility(View.GONE);
            stopAction.setText("停止");
            stopAction.setTextColor(WHITE);
            stopAction.setBackground(rounded(RED, dp(14), Color.TRANSPARENT, 0));
            stopAction.setVisibility(View.VISIBLE);
            updateMargins(true, true);
            updateWindowPosition();
            schedulePositionUpdates();
            visibleMode = "remote";
            visibleState = "正在运行";
            visiblePaused = paused;
        });
    }

    void showError(String message) {
        showSheet("需要处理", message == null || message.trim().isEmpty() ? "操作没有继续" : message.trim(), "", RED);
    }

    private void showSheet(String heading, String message, String detail, int accent) {
        mainHandler.post(() -> {
            if (root == null) {
                if (service != null) attach(service);
                return;
            }
            mainHandler.removeCallbacks(compactRunnable);
            root.setOrientation(LinearLayout.HORIZONTAL);
            root.setGravity(Gravity.CENTER_VERTICAL);
            root.setPadding(dp(12), dp(8), dp(8), dp(8));
            root.setMinimumWidth(dp(352));
            root.setBackground(rounded(WHITE, dp(20), Color.argb(34, 0, 0, 0), dp(1)));
            statusDot.setText("●");
            statusDot.setTextColor(accent);
            title.setText(heading);
            title.setTextColor(INK);
            title.setTextSize(16);
            subtitle.setText(composeSubtitle(message, detail));
            subtitle.setTextColor(MUTED);
            subtitle.setTextSize(12);
            inputAction.setVisibility(View.VISIBLE);
            voiceAction.setVisibility(View.VISIBLE);
            soundAction.setVisibility(View.VISIBLE);
            trailing.setVisibility(View.GONE);
            stopAction.setText("停止");
            stopAction.setTextColor(WHITE);
            stopAction.setBackground(rounded(RED, dp(14), Color.TRANSPARENT, 0));
            stopAction.setVisibility(View.VISIBLE);
            updateMargins(true, true);
            updateWindowPosition();
            schedulePositionUpdates();
            mainHandler.postDelayed(compactRunnable, COMPACT_DELAY_MS);
            visibleMode = "sheet";
            visibleState = heading + "\n" + composeSubtitle(message, detail);
            visiblePaused = false;
        });
    }

    private void buildView(AgentAccessibilityService service) {
        root = new LinearLayout(service);
        root.setClipToOutline(false);
        root.setClickable(true);
        root.setElevation(dp(8));
        root.setOnClickListener(view -> showCompact("正在运行"));
        root.setOnTouchListener(this::handleDrag);

        statusDot = new TextView(service);
        statusDot.setTextSize(13);
        statusDot.setGravity(Gravity.CENTER);
        root.addView(statusDot, new LinearLayout.LayoutParams(dp(16), dp(24)));

        LinearLayout textStack = new LinearLayout(service);
        textStack.setOrientation(LinearLayout.VERTICAL);
        textStack.setGravity(Gravity.CENTER_VERTICAL);
        title = new TextView(service);
        title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        title.setSingleLine(true);
        title.setIncludeFontPadding(false);
        subtitle = new TextView(service);
        subtitle.setMaxLines(2);
        subtitle.setIncludeFontPadding(false);
        textStack.addView(title);
        textStack.addView(subtitle);
        root.addView(textStack);

        inputAction = capsuleButton(service, "输入", Color.rgb(86, 101, 111));
        inputAction.setContentDescription("文字输入");
        inputAction.setOnClickListener(view -> showError("文字输入会写回同一对话。"));
        root.addView(inputAction);

        voiceAction = capsuleButton(service, "语音", BLUE);
        voiceAction.setContentDescription("语音输入");
        voiceAction.setOnClickListener(view -> showError("语音输入待接入，默认静音。"));
        root.addView(voiceAction);

        soundAction = capsuleButton(service, "静音", MUTED);
        soundAction.setContentDescription("静音");
        soundAction.setOnClickListener(view -> showError("声音默认关闭。"));
        root.addView(soundAction);

        trailing = capsuleButton(service, "透传", GREEN);
        trailing.setContentDescription("透传状态");
        trailing.setOnClickListener(view -> showCompact("正在运行"));
        root.addView(trailing);

        stopAction = capsuleButton(service, "停止", RED);
        stopAction.setContentDescription("停止");
        stopAction.setOnClickListener(view -> {
            if (this.service != null) {
                this.service.stopService(new Intent(this.service, AgentNodeService.class));
            }
            detach();
        });
        root.addView(stopAction);
    }

    private void removeRoot(boolean clearService) {
        mainHandler.removeCallbacks(positionRunnable);
        if (windowManager != null && root != null) {
            try {
                windowManager.removeView(root);
            } catch (Exception ignored) {
            }
        }
        root = null;
        statusDot = null;
        title = null;
        subtitle = null;
        inputAction = null;
        voiceAction = null;
        soundAction = null;
        trailing = null;
        stopAction = null;
        params = null;
        visibleMode = "";
        visibleState = "";
        visiblePaused = false;
        if (clearService) {
            windowManager = null;
            service = null;
            manuallyPositioned = false;
        }
    }

    private void schedulePositionUpdates() {
        mainHandler.removeCallbacks(positionRunnable);
        if (root != null) {
            mainHandler.postDelayed(positionRunnable, POSITION_INTERVAL_MS);
        }
    }

    private void updateWindowPosition() {
        if (root == null || params == null || windowManager == null) return;
        if (manuallyPositioned) return;
        int targetY = dp(shouldAvoidKeyboard() ? KEYBOARD_BOTTOM_OFFSET_DP : NORMAL_BOTTOM_OFFSET_DP);
        if (params.y == targetY) return;
        params.y = targetY;
        try {
            windowManager.updateViewLayout(root, params);
        } catch (Exception ignored) {
        }
    }

    private boolean shouldAvoidKeyboard() {
        if (service == null) return false;
        try {
            JSONObject focused = service.focusedInputSummary();
            return focused.optBoolean("found", false) && focused.optBoolean("editable", false);
        } catch (Exception ignored) {
            return false;
        }
    }

    private boolean handleDrag(View view, MotionEvent event) {
        if (params == null || windowManager == null || root == null) return false;
        if (event.getAction() == MotionEvent.ACTION_DOWN) {
            dragging = false;
            downRawX = event.getRawX();
            downRawY = event.getRawY();
            int[] location = new int[2];
            root.getLocationOnScreen(location);
            downX = location[0];
            downY = location[1];
            return true;
        }
        if (event.getAction() == MotionEvent.ACTION_MOVE) {
            int dx = Math.round(event.getRawX() - downRawX);
            int dy = Math.round(event.getRawY() - downRawY);
            if (!dragging && Math.abs(dx) < dp(6) && Math.abs(dy) < dp(6)) {
                return false;
            }
            dragging = true;
            manuallyPositioned = true;
            params.gravity = Gravity.TOP | Gravity.LEFT;
            params.x = clamp(downX + dx, dp(8), Math.max(dp(8), screenWidth() - root.getWidth() - dp(8)));
            params.y = clamp(downY + dy, dp(12), Math.max(dp(12), screenHeight() - root.getHeight() - dp(24)));
            try {
                windowManager.updateViewLayout(root, params);
            } catch (Exception ignored) {
            }
            return true;
        }
        if (event.getAction() == MotionEvent.ACTION_UP || event.getAction() == MotionEvent.ACTION_CANCEL) {
            if (dragging) {
                dragging = false;
                return true;
            }
            if (event.getAction() == MotionEvent.ACTION_UP) {
                view.performClick();
            }
            return true;
        }
        return true;
    }

    private void updateMargins(boolean sheet, boolean actionsVisible) {
        View textStack = root.getChildAt(1);
        int textWidth = sheet
                ? dp(92)
                : dp(84);
        LinearLayout.LayoutParams textParams = new LinearLayout.LayoutParams(textWidth, WindowManager.LayoutParams.WRAP_CONTENT);
        textParams.setMargins(dp(5), 0, dp(6), 0);
        textStack.setLayoutParams(textParams);

        int actionSize = actionsVisible ? dp(36) : 0;
        int actionGap = actionsVisible ? dp(5) : 0;
        LinearLayout.LayoutParams inputParams = new LinearLayout.LayoutParams(actionSize, dp(30));
        inputParams.setMargins(0, 0, actionGap, 0);
        inputAction.setLayoutParams(inputParams);

        LinearLayout.LayoutParams voiceParams = new LinearLayout.LayoutParams(actionSize, dp(30));
        voiceParams.setMargins(0, 0, actionGap, 0);
        voiceAction.setLayoutParams(voiceParams);

        LinearLayout.LayoutParams soundParams = new LinearLayout.LayoutParams(actionSize, dp(30));
        soundParams.setMargins(0, 0, actionGap, 0);
        soundAction.setLayoutParams(soundParams);

        LinearLayout.LayoutParams trailingParams = new LinearLayout.LayoutParams(actionSize, dp(30));
        trailingParams.setMargins(0, 0, actionGap, 0);
        trailing.setLayoutParams(trailingParams);

        LinearLayout.LayoutParams stopParams = new LinearLayout.LayoutParams(actionSize, dp(30));
        stopAction.setLayoutParams(stopParams);
    }

    private TextView capsuleButton(android.content.Context context, String text, int color) {
        TextView view = new TextView(context);
        view.setText(text);
        view.setTextColor(WHITE);
        view.setTextSize(11);
        view.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        view.setGravity(Gravity.CENTER);
        view.setSingleLine(true);
        view.setIncludeFontPadding(false);
        view.setBackground(rounded(color, dp(14), Color.TRANSPARENT, 0));
        view.setPadding(0, 0, 0, 0);
        return view;
    }

    private String composeSubtitle(String message, String detail) {
        if (detail == null || detail.trim().isEmpty()) return message;
        return message + " · " + detail.trim();
    }

    private GradientDrawable rounded(int color, int radiusPx, int strokeColor, int strokeWidthPx) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(radiusPx);
        drawable.setStroke(strokeWidthPx, strokeColor);
        return drawable;
    }

    private int dp(int value) {
        if (service == null) return value;
        return Math.round(value * service.getResources().getDisplayMetrics().density);
    }

    private int screenWidth() {
        if (service == null) return 1080;
        DisplayMetrics metrics = service.getResources().getDisplayMetrics();
        return metrics.widthPixels;
    }

    private int screenHeight() {
        if (service == null) return 2400;
        DisplayMetrics metrics = service.getResources().getDisplayMetrics();
        return metrics.heightPixels;
    }

    private int clamp(int value, int min, int max) {
        return Math.max(min, Math.min(max, value));
    }
}
