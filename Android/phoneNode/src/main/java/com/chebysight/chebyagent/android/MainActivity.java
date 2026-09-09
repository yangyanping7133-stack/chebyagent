package com.chebysight.chebyagent.android;

import android.Manifest;
import android.animation.ValueAnimator;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ComponentName;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.RectF;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.TextUtils;
import android.view.Gravity;
import android.view.View;
import android.view.Window;
import android.view.animation.AccelerateDecelerateInterpolator;
import android.view.animation.LinearInterpolator;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

public class MainActivity extends Activity {
    private static final String ACTION_ACCESSIBILITY_DETAILS_SETTINGS = "android.settings.ACCESSIBILITY_DETAILS_SETTINGS";

    private static final int BG = Color.rgb(246, 248, 248);
    private static final int BG_TOP = Color.rgb(250, 252, 250);
    private static final int BG_BOTTOM = Color.rgb(237, 242, 246);
    private static final int CARD = Color.argb(246, 255, 255, 255);
    private static final int INK = Color.rgb(47, 58, 64);
    private static final int MUTED = Color.rgb(112, 122, 130);
    private static final int QUIET = Color.rgb(142, 142, 147);
    private static final int GREEN = Color.rgb(52, 199, 89);
    private static final int GREEN_SOFT = Color.rgb(226, 249, 235);
    private static final int GREEN_INK = Color.rgb(19, 118, 52);
    private static final int TEAL = Color.rgb(63, 81, 88);
    private static final int BLUE = Color.rgb(10, 132, 255);
    private static final int RED = Color.rgb(255, 59, 48);
    private static final int RED_SOFT = Color.rgb(255, 232, 230);
    private static final int STROKE = Color.argb(26, 28, 28, 30);

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Runnable refreshRunnable = new Runnable() {
        @Override
        public void run() {
            refresh();
            mainHandler.postDelayed(this, 1600L);
        }
    };

    private LinearLayout powerOrb;
    private NodeGlyphView powerGlyph;
    private TextView powerCaption;
    private TextView flowStatus;
    private ArchitectureFlowView architectureFlow;
    private TextView recentSummary;
    private TextView recentMeta;
    private TextView recentStatus;
    private Button accessibilityButton;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        styleSystemBars();
        maybeRequestNotificationPermission();
        AuditLog.append(this, "app_open", JsonUtil.obj("screen", "main"));
        startAgentNode();
        buildUi();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (PendingTargetLaunch.dispatchIfPending(this)) return;
        refresh();
        mainHandler.postDelayed(refreshRunnable, 1600L);
    }

    @Override
    protected void onPause() {
        mainHandler.removeCallbacks(refreshRunnable);
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
    }

    private void buildUi() {
        ScrollView scrollView = new ScrollView(this);
        scrollView.setFillViewport(true);
        scrollView.setClipToPadding(false);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER_HORIZONTAL);
        root.setPadding(dp(22), dp(28), dp(22), dp(22));
        root.setBackground(screenBackground());
        scrollView.addView(root, new ScrollView.LayoutParams(
                ScrollView.LayoutParams.MATCH_PARENT,
                ScrollView.LayoutParams.MATCH_PARENT
        ));

        root.addView(brandHeader());
        root.addView(space(18));

        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setGravity(Gravity.CENTER_HORIZONTAL);
        card.setPadding(dp(18), dp(20), dp(18), dp(18));
        card.setBackground(rounded(CARD, 32, STROKE, 1));
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) card.setElevation(dp(8));
        LinearLayout.LayoutParams cardParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        cardParams.setMargins(0, 0, 0, dp(16));
        root.addView(card, cardParams);

        TextView powerTitle = text("远端控制", 16, INK, true);
        powerTitle.setGravity(Gravity.CENTER);
        card.addView(powerTitle);
        card.addView(space(12));

        powerOrb = new LinearLayout(this);
        powerOrb.setOrientation(LinearLayout.VERTICAL);
        powerOrb.setGravity(Gravity.CENTER);
        powerOrb.setClickable(true);
        powerOrb.setFocusable(true);
        powerOrb.setContentDescription("远端控制");
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) powerOrb.setElevation(dp(10));

        powerGlyph = new NodeGlyphView(this);
        powerOrb.addView(powerGlyph, new LinearLayout.LayoutParams(dp(64), dp(64)));
        View.OnClickListener remoteClick = view -> {
            if (AgentNodeService.isRunning()) {
                stopAgentNode();
            } else {
                startAgentNode();
            }
            refreshSoon();
            pulse(powerOrb);
        };
        powerOrb.setOnClickListener(remoteClick);
        LinearLayout.LayoutParams powerParams = new LinearLayout.LayoutParams(
                dp(128),
                dp(128)
        );
        powerParams.gravity = Gravity.CENTER_HORIZONTAL;
        card.addView(powerOrb, powerParams);
        powerCaption = text("点击开启", 14, MUTED, true);
        powerCaption.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams powerCaptionParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        powerCaptionParams.setMargins(0, dp(14), 0, dp(18));
        card.addView(powerCaption, powerCaptionParams);

        root.addView(space(12));

        LinearLayout architecture = architecturePanel();
        root.addView(architecture, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                dp(178)
        ));
        root.addView(space(12));
        root.addView(recentPanel(), new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        ));
        root.addView(space(12));

        accessibilityButton = button("打开手机控制权限", view -> openAccessibilityControl());
        root.addView(accessibilityButton);

        setContentView(scrollView);
        startEntrance(card);
        refresh();
    }

    private void startAgentNode() {
        AuditLog.append(this, "service_start_requested", JsonUtil.obj("source", "main_screen"));
        Intent intent = new Intent(this, AgentNodeService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent);
        } else {
            startService(intent);
        }
    }

    private void stopAgentNode() {
        AuditLog.append(this, "service_stop_requested", JsonUtil.obj("source", "main_screen"));
        AgentNodeService.setPaused(false);
        stopService(new Intent(this, AgentNodeService.class));
    }

    private void refreshSoon() {
        mainHandler.postDelayed(this::refresh, 220L);
    }

    private void refresh() {
        boolean running = AgentNodeService.isRunning();
        boolean accessibilityEnabled = AccessibilityState.isServiceEnabled(this);
        boolean accessibilityBound = AgentAccessibilityService.getInstance() != null;
        boolean live = running && accessibilityEnabled && accessibilityBound;

        if (!running) {
            powerCaption.setText("点击开启");
            updatePowerOrb(QUIET);
        } else if (!accessibilityEnabled || !accessibilityBound) {
            powerCaption.setText("需要控制权限");
            updatePowerOrb(TEAL);
        } else {
            powerCaption.setText("点击关闭");
            updatePowerOrb(TEAL);
        }

        if (flowStatus != null) {
            JSONObject bridge = PhoneBridgeClient.snapshot();
            String bridgeConnection = bridge.optString("connection", "");
            if (!running) {
                setChip(flowStatus, "未开启", Color.rgb(238, 241, 246), MUTED);
            } else if (!accessibilityEnabled || !accessibilityBound) {
                setChip(flowStatus, "需授权", RED_SOFT, RED);
            } else if (!"connected".equals(bridgeConnection) && !"executing".equals(bridgeConnection)) {
                setChip(flowStatus, "连接中", Color.rgb(238, 241, 246), MUTED);
            } else {
                setChip(flowStatus, "在线", GREEN_SOFT, GREEN_INK);
            }
        }
        if (architectureFlow != null) architectureFlow.setLive(live);

        refreshRecent();
        if (accessibilityButton != null) {
            accessibilityButton.setText((!accessibilityEnabled || !accessibilityBound)
                    ? "打开手机控制权限"
                    : "管理手机控制权限");
            accessibilityButton.setVisibility(View.VISIBLE);
        }
    }

    private void openAccessibilityControl() {
        ComponentName component = new ComponentName(this, AgentAccessibilityService.class);
        AuditLog.append(this, "accessibility_control_requested", JsonUtil.obj(
                "enabled", AccessibilityState.isServiceEnabled(this),
                "bound", AgentAccessibilityService.getInstance() != null
        ));

        Intent details = new Intent(ACTION_ACCESSIBILITY_DETAILS_SETTINGS);
        details.putExtra(Intent.EXTRA_COMPONENT_NAME, component.flattenToString());
        if (tryStartAccessibilityIntent(details, "details")) return;

        Intent settings = new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS);
        settings.putExtra(Intent.EXTRA_COMPONENT_NAME, component.flattenToString());
        if (tryStartAccessibilityIntent(settings, "list")) return;

        Intent appSettings = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS);
        appSettings.setData(Uri.parse("package:" + getPackageName()));
        tryStartAccessibilityIntent(appSettings, "app_settings");
    }

    private boolean tryStartAccessibilityIntent(Intent intent, String route) {
        try {
            startActivity(intent);
            AuditLog.append(this, "accessibility_settings_opened", JsonUtil.obj("route", route));
            return true;
        } catch (ActivityNotFoundException | SecurityException error) {
            AuditLog.append(this, "accessibility_settings_open_failed", JsonUtil.obj(
                    "route", route,
                    "error", error.getMessage() == null ? String.valueOf(error) : error.getMessage()
            ));
            if ("app_settings".equals(route)) {
                Toast.makeText(this, "无法打开系统设置", Toast.LENGTH_SHORT).show();
            }
            return false;
        }
    }

    private LinearLayout architecturePanel() {
        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setPadding(dp(18), dp(16), dp(18), dp(14));
        panel.setBackground(rounded(Color.argb(236, 255, 255, 255), 30, STROKE, 1));
        panel.setClickable(true);
        panel.setFocusable(true);
        panel.setContentDescription("查看最近记录");
        panel.setOnClickListener(view -> {
            pulse(panel);
            showAuditLog();
        });
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) panel.setElevation(dp(5));

        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.HORIZONTAL);
        header.setGravity(Gravity.CENTER_VERTICAL);

        TextView title = text("实时链路", 15, INK, true);
        header.addView(title, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));

        flowStatus = chip("待连接", Color.rgb(238, 241, 246), MUTED);
        header.addView(flowStatus);
        panel.addView(header);

        architectureFlow = new ArchitectureFlowView(this);
        LinearLayout.LayoutParams flowParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                0,
                1f
        );
        flowParams.setMargins(0, dp(8), 0, 0);
        panel.addView(architectureFlow, flowParams);
        return panel;
    }

    private LinearLayout recentPanel() {
        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setMinimumHeight(dp(92));
        panel.setPadding(dp(18), dp(14), dp(18), dp(14));
        panel.setBackground(rounded(Color.argb(230, 255, 255, 255), 26, STROKE, 1));
        panel.setClickable(true);
        panel.setFocusable(true);
        panel.setContentDescription("查看最近记录");
        panel.setOnClickListener(view -> {
            pulse(panel);
            showAuditLog();
        });
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) panel.setElevation(dp(3));

        LinearLayout top = new LinearLayout(this);
        top.setOrientation(LinearLayout.HORIZONTAL);
        top.setGravity(Gravity.CENTER_VERTICAL);
        TextView title = text("最近记录", 14, INK, true);
        top.addView(title, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        recentStatus = chip("记录", Color.rgb(238, 241, 246), MUTED);
        top.addView(recentStatus);
        panel.addView(top);

        recentSummary = text("暂无记录", 15, INK, true);
        recentSummary.setSingleLine(true);
        recentSummary.setEllipsize(TextUtils.TruncateAt.END);
        recentSummary.setGravity(Gravity.CENTER_VERTICAL);
        LinearLayout.LayoutParams summaryParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        summaryParams.setMargins(0, dp(9), 0, 0);
        panel.addView(recentSummary, summaryParams);

        recentMeta = text("--:--:--", 12, MUTED, false);
        LinearLayout.LayoutParams metaParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        metaParams.setMargins(0, dp(4), 0, 0);
        panel.addView(recentMeta, metaParams);
        return panel;
    }

    private void refreshRecent() {
        if (recentSummary == null || recentMeta == null || recentStatus == null) return;
        JSONArray events = AuditLog.recent(this, 1);
        JSONObject event = events.length() > 0 ? events.optJSONObject(0) : null;
        if (event == null) {
            recentSummary.setText("暂无记录");
            recentMeta.setText("--:--:--");
            setChip(recentStatus, "记录", Color.rgb(238, 241, 246), MUTED);
            return;
        }
        String status = AuditLog.statusLabel(event);
        recentSummary.setText(AuditLog.summary(event));
        recentMeta.setText(formatAuditTime(event.optLong("created_at", 0L)));
        setChip(recentStatus, status, auditStatusBg(status), auditStatusInk(status));
    }

    private void setChip(TextView chip, String value, int color, int textColor) {
        chip.setText(value);
        chip.setTextColor(textColor);
        chip.setBackground(rounded(color, 999, Color.TRANSPARENT, 0));
    }

    private void updatePowerOrb(int color) {
        powerOrb.setBackground(rounded(color, 999, Color.TRANSPARENT, 0));
        if (powerGlyph != null) powerGlyph.invalidate();
    }

    private Button button(String label, View.OnClickListener listener) {
        Button button = new Button(this);
        button.setText(label);
        button.setTextColor(Color.WHITE);
        button.setAllCaps(false);
        button.setGravity(Gravity.CENTER);
        button.setTextSize(15);
        button.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        button.setBackground(rounded(BLUE, 18, Color.TRANSPARENT, 0));
        button.setOnClickListener(listener);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                dp(48)
        );
        params.setMargins(0, 0, 0, dp(10));
        button.setLayoutParams(params);
        return button;
    }

    private LinearLayout brandHeader() {
        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.HORIZONTAL);
        header.setGravity(Gravity.CENTER_VERTICAL);
        header.setPadding(dp(3), dp(3), dp(3), dp(3));

        LinearLayout names = new LinearLayout(this);
        names.setOrientation(LinearLayout.VERTICAL);
        names.setGravity(Gravity.CENTER_VERTICAL);
        TextView title = text("ChebyNode", 29, INK, true);
        TextView brandLine = text("CRC Research Center", 13, MUTED, true);
        LinearLayout.LayoutParams brandParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        brandParams.setMargins(0, dp(3), 0, 0);
        names.addView(title);
        names.addView(brandLine, brandParams);
        LinearLayout.LayoutParams nameParams = new LinearLayout.LayoutParams(
                0,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                1f
        );
        nameParams.setMargins(0, 0, dp(12), 0);
        header.addView(names, nameParams);

        return header;
    }

    private TextView text(String value, int sp, int color, boolean bold) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(sp);
        view.setTextColor(color);
        if (bold) view.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        return view;
    }

    private TextView chip(String value, int color, int textColor) {
        TextView view = text(value, 12, textColor, true);
        view.setGravity(Gravity.CENTER);
        view.setMinWidth(dp(56));
        view.setPadding(dp(10), dp(5), dp(10), dp(5));
        view.setBackground(rounded(color, 999, Color.TRANSPARENT, 0));
        view.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        ));
        return view;
    }

    private View space(int heightDp) {
        View view = new View(this);
        view.setLayoutParams(new LinearLayout.LayoutParams(1, dp(heightDp)));
        return view;
    }

    private View spaceWidth(int widthDp) {
        View view = new View(this);
        view.setLayoutParams(new LinearLayout.LayoutParams(dp(widthDp), 1));
        return view;
    }

    private GradientDrawable screenBackground() {
        return new GradientDrawable(
                GradientDrawable.Orientation.TOP_BOTTOM,
                new int[]{BG_TOP, BG_BOTTOM}
        );
    }

    private GradientDrawable rounded(int color, int radiusDp, int strokeColor, int strokeDp) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(dp(radiusDp));
        if (strokeDp > 0) drawable.setStroke(dp(strokeDp), strokeColor);
        return drawable;
    }

    private void startEntrance(View view) {
        view.setAlpha(0f);
        view.setTranslationY(dp(10));
        view.animate()
                .alpha(1f)
                .translationY(0f)
                .setDuration(360L)
                .setInterpolator(new AccelerateDecelerateInterpolator())
                .start();
    }

    private void pulse(View view) {
        view.animate()
                .scaleX(0.96f)
                .scaleY(0.96f)
                .setDuration(80L)
                .withEndAction(() -> view.animate().scaleX(1f).scaleY(1f).setDuration(140L).start())
                .start();
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private float sp(int value) {
        return value * getResources().getDisplayMetrics().scaledDensity;
    }

    private void showAuditLog() {
        JSONArray events = AuditLog.recent(this);

        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(dp(18), dp(10), dp(18), dp(4));

        TextView subtitle = text("最近 20 条本机记录，已自动脱敏", 13, MUTED, false);
        content.addView(subtitle);
        content.addView(space(12));

        if (events.length() == 0) {
            TextView empty = text("暂无记录", 15, QUIET, true);
            empty.setGravity(Gravity.CENTER);
            empty.setPadding(0, dp(34), 0, dp(34));
            content.addView(empty, new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT
            ));
        } else {
            for (int i = 0; i < events.length(); i++) {
                JSONObject event = events.optJSONObject(i);
                if (event == null) continue;
                LinearLayout.LayoutParams rowParams = new LinearLayout.LayoutParams(
                        LinearLayout.LayoutParams.MATCH_PARENT,
                        LinearLayout.LayoutParams.WRAP_CONTENT
                );
                rowParams.setMargins(0, 0, 0, dp(9));
                content.addView(auditRow(event), rowParams);
            }
        }

        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(false);
        scroll.addView(content, new ScrollView.LayoutParams(
                ScrollView.LayoutParams.MATCH_PARENT,
                ScrollView.LayoutParams.WRAP_CONTENT
        ));

        new AlertDialog.Builder(this)
                .setTitle("最近记录")
                .setView(scroll)
                .setNegativeButton("清空", (dialog, which) -> AuditLog.clear(this))
                .setPositiveButton("关闭", null)
                .show();
    }

    private LinearLayout auditRow(JSONObject event) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.VERTICAL);
        row.setPadding(dp(13), dp(11), dp(13), dp(11));
        row.setBackground(rounded(Color.rgb(252, 251, 248), 18, STROKE, 1));

        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.HORIZONTAL);
        header.setGravity(Gravity.CENTER_VERTICAL);
        TextView time = text(formatAuditTime(event.optLong("created_at", 0L)), 12, QUIET, false);
        header.addView(time, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));

        String status = AuditLog.statusLabel(event);
        header.addView(chip(status, auditStatusBg(status), auditStatusInk(status)));
        row.addView(header);

        TextView summary = text(AuditLog.summary(event), 15, INK, true);
        summary.setLineSpacing(dp(2), 1f);
        LinearLayout.LayoutParams summaryParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        summaryParams.setMargins(0, dp(7), 0, 0);
        row.addView(summary, summaryParams);

        TextView type = text(event.optString("type", "event"), 12, MUTED, false);
        LinearLayout.LayoutParams typeParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        typeParams.setMargins(0, dp(5), 0, 0);
        row.addView(type, typeParams);
        return row;
    }

    private String formatAuditTime(long createdAt) {
        if (createdAt <= 0L) return "--:--:--";
        return new SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(new Date(createdAt));
    }

    private int auditStatusBg(String status) {
        if ("失败".equals(status)) return RED_SOFT;
        if ("成功".equals(status)) return GREEN_SOFT;
        return Color.rgb(238, 241, 246);
    }

    private int auditStatusInk(String status) {
        if ("失败".equals(status)) return RED;
        if ("成功".equals(status)) return GREEN_INK;
        return MUTED;
    }

    private final class ArchitectureFlowView extends View {
        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private ValueAnimator animator;
        private float progress = 0f;
        private boolean live = false;

        ArchitectureFlowView(Activity context) {
            super(context);
            setWillNotDraw(false);
        }

        void setLive(boolean value) {
            if (live == value) return;
            live = value;
            invalidate();
        }

        @Override
        protected void onAttachedToWindow() {
            super.onAttachedToWindow();
            animator = ValueAnimator.ofFloat(0f, 1f);
            animator.setDuration(1800L);
            animator.setRepeatCount(ValueAnimator.INFINITE);
            animator.setInterpolator(new LinearInterpolator());
            animator.addUpdateListener(animation -> {
                progress = (float) animation.getAnimatedValue();
                invalidate();
            });
            animator.start();
        }

        @Override
        protected void onDetachedFromWindow() {
            if (animator != null) {
                animator.cancel();
                animator = null;
            }
            super.onDetachedFromWindow();
        }

        @Override
        protected void onDraw(Canvas canvas) {
            super.onDraw(canvas);
            float width = getWidth();
            float height = getHeight();
            if (width <= 0 || height <= 0) return;

            float left = dp(28);
            float right = width - dp(28);
            float railY = height * 0.42f;
            float labelY = railY + dp(36);
            float gap = (right - left) / 3f;
            float[] xs = new float[]{left, left + gap, left + gap * 2f, right};
            String[] labels = new String[]{"飞书", "OpenClaw", "Bridge", "手机"};

            drawRail(canvas, xs[0], xs[3], railY);
            if (live) drawFlowHighlight(canvas, xs[0], xs[3], railY);

            for (int i = 0; i < xs.length; i++) {
                drawFlowNode(canvas, xs[i], railY, labelY, labels[i], i == 2);
            }
        }

        private void drawRail(Canvas canvas, float x1, float x2, float y) {
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeCap(Paint.Cap.ROUND);
            paint.setStrokeWidth(dp(3));
            paint.setColor(live ? Color.argb(96, 63, 81, 88) : Color.argb(62, 122, 132, 138));
            canvas.drawLine(x1, y, x2, y, paint);
        }

        private void drawFlowHighlight(Canvas canvas, float x1, float x2, float y) {
            float travel = x2 - x1;
            float head = x1 + travel * progress;
            float tail = Math.max(x1, head - dp(72));
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeCap(Paint.Cap.ROUND);
            paint.setStrokeWidth(dp(6));
            paint.setColor(Color.argb(24, 52, 199, 89));
            canvas.drawLine(tail, y, head, y, paint);
            paint.setStrokeWidth(dp(3));
            paint.setColor(Color.argb(82, 52, 199, 89));
            canvas.drawLine(tail, y, head, y, paint);
        }

        private void drawFlowNode(Canvas canvas, float cx, float cy, float labelY, String title, boolean current) {
            float radius = current ? dp(11) : dp(9);
            if (current && live) {
                paint.setStyle(Paint.Style.STROKE);
                paint.setStrokeWidth(dp(2));
                paint.setColor(Color.argb(38, 52, 199, 89));
                canvas.drawCircle(cx, cy, dp(20), paint);
            }

            paint.setStyle(Paint.Style.FILL);
            paint.setColor(current ? Color.rgb(235, 250, 241) : Color.rgb(252, 251, 248));
            canvas.drawCircle(cx, cy, radius, paint);
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeWidth(dp(2));
            paint.setColor(current ? Color.argb(130, 52, 199, 89) : Color.argb(72, 75, 88, 94));
            canvas.drawCircle(cx, cy, radius, paint);

            paint.setStyle(Paint.Style.FILL);
            paint.setTextAlign(Paint.Align.CENTER);
            paint.setTypeface(current ? Typeface.DEFAULT_BOLD : Typeface.DEFAULT);
            paint.setTextSize(sp(12));
            paint.setColor(current ? GREEN_INK : MUTED);
            canvas.drawText(title, cx, labelY, paint);
        }
    }

    private final class NodeGlyphView extends View {
        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final RectF phone = new RectF();

        NodeGlyphView(Activity context) {
            super(context);
            setWillNotDraw(false);
        }

        @Override
        protected void onDraw(Canvas canvas) {
            super.onDraw(canvas);
            float width = getWidth();
            float height = getHeight();
            if (width <= 0 || height <= 0) return;

            float cx = width * 0.5f;
            float cy = height * 0.5f;
            float phoneW = width * 0.31f;
            float phoneH = height * 0.48f;
            float leftNodeX = cx - width * 0.31f;
            float leftNodeY = cy + height * 0.16f;
            float rightNodeX = cx + width * 0.31f;
            float rightNodeY = cy - height * 0.16f;
            phone.set(cx - phoneW / 2f, cy - phoneH / 2f, cx + phoneW / 2f, cy + phoneH / 2f);

            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeCap(Paint.Cap.ROUND);
            paint.setStrokeWidth(dp(3));
            paint.setColor(Color.argb(188, 255, 250, 238));
            canvas.drawLine(leftNodeX, leftNodeY, phone.left, cy + height * 0.11f, paint);
            canvas.drawLine(phone.right, cy - height * 0.11f, rightNodeX, rightNodeY, paint);
            canvas.drawRoundRect(phone, dp(7), dp(7), paint);

            paint.setStrokeWidth(dp(2));
            paint.setColor(Color.argb(92, 255, 250, 238));
            canvas.drawLine(cx - phoneW * 0.18f, phone.bottom - dp(5), cx + phoneW * 0.18f, phone.bottom - dp(5), paint);

            paint.setStyle(Paint.Style.FILL);
            paint.setColor(Color.rgb(255, 250, 238));
            canvas.drawCircle(leftNodeX, leftNodeY, dp(5), paint);
            canvas.drawCircle(rightNodeX, rightNodeY, dp(5), paint);

            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeWidth(dp(2));
            paint.setColor(Color.argb(56, 255, 250, 238));
            canvas.drawCircle(cx, cy, width * 0.40f, paint);
        }
    }

    private void maybeRequestNotificationPermission() {
        if (Build.VERSION.SDK_INT < 33) return;
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) return;
        requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1002);
    }

    private void styleSystemBars() {
        Window window = getWindow();
        window.setStatusBarColor(BG);
        window.setNavigationBarColor(BG);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            window.getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR);
        }
    }
}
