package com.chebysight.chebyagent.android;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.accessibilityservice.GestureDescription;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Path;
import android.graphics.Rect;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Base64;
import android.view.Display;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

public class AgentAccessibilityService extends AccessibilityService {
    private static final int SCREENSHOT_MAX_LONG_EDGE = 1280;
    private static final int SCREENSHOT_MAX_ENCODED_BYTES = 512 * 1024;
    private static final int SCREENSHOT_INITIAL_JPEG_QUALITY = 75;
    private static final int SCREENSHOT_MIN_JPEG_QUALITY = 45;
    private static final float SCREENSHOT_DOWNSCALE_STEP = 0.85f;
    private static volatile AgentAccessibilityService instance;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private volatile String lastPackageName = "";
    private volatile String lastClassName = "";
    private volatile long lastEventAt = 0L;
    private String overlaySyncedPackageName = "";
    private boolean overlaySyncedPaused = false;

    static AgentAccessibilityService getInstance() {
        return instance;
    }

    @Override
    protected void onServiceConnected() {
        instance = this;
        AccessibilityServiceInfo info = getServiceInfo();
        if (info != null) {
            info.flags |= AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;
            info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
            setServiceInfo(info);
        }
        AgentNodeService.ensureStarted(this, "accessibility_service_connected");
        hideAgentOverlay();
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        if (event == null) return;
        CharSequence pkg = event.getPackageName();
        CharSequence cls = event.getClassName();
        lastPackageName = pkg == null ? "" : pkg.toString();
        lastClassName = cls == null ? "" : cls.toString();
        lastEventAt = System.currentTimeMillis();
        if (getPackageName().equals(lastPackageName)) {
            hideAgentOverlay();
        }
    }

    @Override
    public void onInterrupt() {
    }

    @Override
    public boolean onUnbind(android.content.Intent intent) {
        PendingTargetLaunch.cancelAll("Accessibility service was unbound during target launch.");
        AgentOverlayController.get().detach();
        instance = null;
        return super.onUnbind(intent);
    }

    void showAgentError(String message) {
        AgentOverlayController.get().attach(this);
        AgentOverlayController.get().showError(message);
    }

    void showAgentRunningOverlay() {
        boolean paused = false;
        if (getPackageName().equals(lastPackageName)) {
            if (!getPackageName().equals(overlaySyncedPackageName)) {
                AgentOverlayController.get().detach();
                overlaySyncedPackageName = getPackageName();
            }
            overlaySyncedPaused = paused;
            return;
        }
        if (AgentOverlayController.get().isVisible()
                && lastPackageName.equals(overlaySyncedPackageName)
                && paused == overlaySyncedPaused) {
            return;
        }
        AgentOverlayController.get().attach(this);
        AgentOverlayController.get().showCompact("已启动");
        overlaySyncedPackageName = lastPackageName;
        overlaySyncedPaused = paused;
    }

    void hideAgentOverlay() {
        AgentOverlayController.get().detach();
    }

    JSONObject runtimeStatus() throws Exception {
        return JsonUtil.obj(
                "bound", true,
                "last_package_name", lastPackageName,
                "last_class_name", lastClassName,
                "last_event_at", lastEventAt
        );
    }

    JSONObject captureUiTree() throws Exception {
        JSONObject output = JsonUtil.obj(
                "captured_at", System.currentTimeMillis(),
                "last_package_name", lastPackageName,
                "last_class_name", lastClassName
        );

        JSONArray windows = new JSONArray();
        if (!isHuaweiBuild() && Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            List<android.view.accessibility.AccessibilityWindowInfo> windowInfos = getWindows();
            for (android.view.accessibility.AccessibilityWindowInfo window : windowInfos) {
                JSONObject windowJson = JsonUtil.obj(
                        "id", window.getId(),
                        "type", window.getType(),
                        "focused", window.isFocused(),
                        "active", window.isActive()
                );
                Rect bounds = new Rect();
                window.getBoundsInScreen(bounds);
                windowJson.put("bounds", rectJson(bounds));
                AccessibilityNodeInfo root = window.getRoot();
                if (root != null) {
                    windowJson.put("root", nodeJson(root, 0));
                    root.recycle();
                }
                windows.put(windowJson);
            }
        }

        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root != null) {
            output.put("root", nodeJson(root, 0));
            root.recycle();
        }
        output.put("windows", windows);
        return output;
    }

    boolean tap(float x, float y) throws Exception {
        Path path = new Path();
        path.moveTo(x, y);
        return dispatch(path, 0, 80);
    }

    boolean swipe(float startX, float startY, float endX, float endY, long durationMs) throws Exception {
        Path path = new Path();
        path.moveTo(startX, startY);
        path.lineTo(endX, endY);
        return dispatch(path, 0, Math.max(80, durationMs));
    }

    boolean pressBack() {
        return performGlobalAction(GLOBAL_ACTION_BACK);
    }

    boolean pressHome() {
        return performGlobalAction(GLOBAL_ACTION_HOME);
    }

    boolean pressRecents() {
        return performGlobalAction(GLOBAL_ACTION_RECENTS);
    }

    boolean openOwnLauncherActivity(long timeoutMs) throws InterruptedException {
        Intent homeIntent = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME);
        String launcherPackage = "";
        String applicationLabel = ownApplicationLabel();
        if (homeIntent.resolveActivity(getPackageManager()) != null) {
            launcherPackage = homeIntent.resolveActivity(getPackageManager()).getPackageName();
        }
        if (!performGlobalAction(GLOBAL_ACTION_HOME)) return false;

        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            AccessibilityNodeInfo root = getRootInActiveWindow();
            if (root != null) {
                try {
                    List<AccessibilityNodeInfo> matches = root.findAccessibilityNodeInfosByText(
                            applicationLabel
                    );
                    for (AccessibilityNodeInfo match : matches) {
                        try {
                            CharSequence nodePackage = match.getPackageName();
                            if (!trustedLauncherNode(
                                    launcherPackage,
                                    nodePackage == null ? "" : nodePackage.toString(),
                                    textOf(match),
                                    descriptionOf(match),
                                    applicationLabel
                            )) continue;
                            if (clickNodeOrParent(match)) {
                                AuditLog.append(this, "own_launcher_icon_clicked", JsonUtil.obj(
                                        "launcher_package", launcherPackage
                                ));
                                return true;
                            }
                        } finally {
                            match.recycle();
                        }
                    }
                } finally {
                    root.recycle();
                }
            }
            Thread.sleep(120L);
        }
        return false;
    }

    boolean awaitForegroundPackage(String expectedPackage, long timeoutMs) throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            if (expectedPackage.equals(currentForegroundPackage())) return true;
            Thread.sleep(100L);
        }
        return expectedPackage.equals(currentForegroundPackage());
    }

    boolean approveOwnMediaProjectionConsent(long timeoutMs) throws Exception {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            JSONObject screen = policyScreenSummary();
            AccessibilityNodeInfo root = getRootInActiveWindow();
            if (root != null) {
                try {
                    List<AccessibilityNodeInfo> matches = root.findAccessibilityNodeInfosByViewId(
                            "android:id/button1"
                    );
                    for (AccessibilityNodeInfo match : matches) {
                        try {
                            if (!PolicyGate.isOwnMediaProjectionConsent(
                                    screen.optString("package_name", ""),
                                    safe(match.getPackageName()),
                                    true,
                                    match.isClickable(),
                                    safe(match.getClassName()),
                                    safe(match.getViewIdResourceName()),
                                    safe(match.getText()),
                                    screen.optString("screen_text", ""),
                                    ownApplicationLabel()
                            )) continue;
                            if (clickNodeOrParent(match)) {
                                AuditLog.append(this, "media_projection_consent_allowed", JsonUtil.obj(
                                        "view_id", "android:id/button1",
                                        "screen_package", screen.optString("package_name", "")
                                ));
                                return true;
                            }
                        } finally {
                            match.recycle();
                        }
                    }
                } finally {
                    root.recycle();
                }
            }
            Thread.sleep(100L);
        }
        return false;
    }

    String currentForegroundPackage() {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root != null) {
            try {
                CharSequence value = root.getPackageName();
                if (value != null && value.length() > 0) return value.toString();
            } finally {
                root.recycle();
            }
        }
        return lastPackageName;
    }

    String ownApplicationLabel() {
        CharSequence label = getApplicationInfo().loadLabel(getPackageManager());
        return safe(label);
    }

    static boolean trustedLauncherNode(
            String launcherPackage,
            String nodePackage,
            String text,
            String contentDescription,
            String applicationLabel
    ) {
        if (launcherPackage == null || launcherPackage.isEmpty()) return false;
        if (!launcherPackage.equals(nodePackage)) return false;
        if (applicationLabel == null || applicationLabel.isEmpty()) return false;
        return applicationLabel.equals(text) || applicationLabel.equals(contentDescription);
    }

    private static String textOf(AccessibilityNodeInfo node) {
        CharSequence value = node.getText();
        return value == null ? "" : value.toString();
    }

    private static String descriptionOf(AccessibilityNodeInfo node) {
        CharSequence value = node.getContentDescription();
        return value == null ? "" : value.toString();
    }

    private static boolean clickNodeOrParent(AccessibilityNodeInfo node) {
        AccessibilityNodeInfo current = AccessibilityNodeInfo.obtain(node);
        try {
            for (int depth = 0; current != null && depth < 8; depth++) {
                if (current.isClickable()
                        && current.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return true;
                AccessibilityNodeInfo parent = current.getParent();
                current.recycle();
                current = parent;
            }
            return false;
        } finally {
            if (current != null) current.recycle();
        }
    }

    boolean inputText(String text) throws Exception {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return false;
        try {
            AccessibilityNodeInfo focused = findFocusedEditableNode(root);
            if (focused == null) return false;
            try {
                Bundle args = new Bundle();
                args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
                return focused.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args);
            } finally {
                focused.recycle();
            }
        } finally {
            root.recycle();
        }
    }

    JSONObject takeTransportScreenshot() throws Exception {
        Bitmap bitmap = captureScreenshotBitmap();
        try {
            EncodedScreenshot encoded = encodeScreenshotForTransport(bitmap);
            try {
                return JsonUtil.obj(
                        "captured_at", System.currentTimeMillis(),
                        "mime_type", "image/jpeg",
                        "format", "jpeg",
                        "width", encoded.bitmap.getWidth(),
                        "height", encoded.bitmap.getHeight(),
                        "screen_width", bitmap.getWidth(),
                        "screen_height", bitmap.getHeight(),
                        "source_width", bitmap.getWidth(),
                        "source_height", bitmap.getHeight(),
                        "coordinate_scale_x", bitmap.getWidth() / (double) encoded.bitmap.getWidth(),
                        "coordinate_scale_y", bitmap.getHeight() / (double) encoded.bitmap.getHeight(),
                        "downscaled_for_transport", encoded.downscaled,
                        "encoded_bytes", encoded.bytes.length,
                        "transport_max_encoded_bytes", SCREENSHOT_MAX_ENCODED_BYTES,
                        "jpeg_quality", encoded.quality,
                        "data_base64", Base64.encodeToString(encoded.bytes, Base64.NO_WRAP)
                );
            } finally {
                encoded.recycle();
            }
        } finally {
            bitmap.recycle();
        }
    }

    JSONObject takePhoneBridgeScreenshot() throws Exception {
        Bitmap bitmap = captureScreenshotBitmap();
        try {
            EncodedScreenshot encoded = encodeScreenshotForTransport(bitmap);
            try {
                return JsonUtil.obj(
                        "captured_at", System.currentTimeMillis(),
                        "mime_type", "image/jpeg",
                        "format", "jpeg",
                        "width", encoded.bitmap.getWidth(),
                        "height", encoded.bitmap.getHeight(),
                        "screen_width", bitmap.getWidth(),
                        "screen_height", bitmap.getHeight(),
                        "source_width", bitmap.getWidth(),
                        "source_height", bitmap.getHeight(),
                        "coordinate_scale_x", bitmap.getWidth() / (double) encoded.bitmap.getWidth(),
                        "coordinate_scale_y", bitmap.getHeight() / (double) encoded.bitmap.getHeight(),
                        "downscaled_for_transport", encoded.downscaled,
                        "phonebridge_full_fidelity", !encoded.downscaled,
                        "encoded_bytes", encoded.bytes.length,
                        "transport_max_encoded_bytes", SCREENSHOT_MAX_ENCODED_BYTES,
                        "jpeg_quality", encoded.quality,
                        "data_base64", Base64.encodeToString(encoded.bytes, Base64.NO_WRAP)
                );
            } finally {
                encoded.recycle();
            }
        } finally {
            bitmap.recycle();
        }
    }

    private Bitmap captureScreenshotBitmap() throws Exception {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            return MediaProjectionScreenshotCapture.captureOrRequest(this);
        }

        CountDownLatch latch = new CountDownLatch(1);
        AtomicReference<Bitmap> bitmapRef = new AtomicReference<>();
        AtomicReference<Integer> errorRef = new AtomicReference<>();

        takeScreenshot(Display.DEFAULT_DISPLAY, getMainExecutor(), new TakeScreenshotCallback() {
            @Override
            public void onSuccess(ScreenshotResult screenshot) {
                Bitmap hardwareBitmap = Bitmap.wrapHardwareBuffer(
                        screenshot.getHardwareBuffer(),
                        screenshot.getColorSpace()
                );
                if (hardwareBitmap != null) {
                    bitmapRef.set(hardwareBitmap.copy(Bitmap.Config.ARGB_8888, false));
                }
                screenshot.getHardwareBuffer().close();
                latch.countDown();
            }

            @Override
            public void onFailure(int errorCode) {
                errorRef.set(errorCode);
                latch.countDown();
            }
        });

        if (!latch.await(10, TimeUnit.SECONDS)) {
            throw new IllegalStateException("Timed out while taking accessibility screenshot.");
        }
        if (errorRef.get() != null) {
            throw new IllegalStateException("Accessibility screenshot failed with code " + errorRef.get());
        }

        Bitmap bitmap = bitmapRef.get();
        if (bitmap == null) throw new IllegalStateException("Accessibility screenshot returned no bitmap.");
        return bitmap;
    }

    private EncodedScreenshot encodeScreenshotForTransport(Bitmap source) {
        Bitmap current = source;
        boolean currentOwned = false;
        boolean downscaled = false;

        int longEdge = Math.max(source.getWidth(), source.getHeight());
        if (longEdge > SCREENSHOT_MAX_LONG_EDGE) {
            double scale = SCREENSHOT_MAX_LONG_EDGE / (double) longEdge;
            current = Bitmap.createScaledBitmap(
                    source,
                    Math.max(1, (int) Math.round(source.getWidth() * scale)),
                    Math.max(1, (int) Math.round(source.getHeight() * scale)),
                    true
            );
            currentOwned = true;
            downscaled = true;
        }

        int quality = SCREENSHOT_INITIAL_JPEG_QUALITY;
        while (true) {
            byte[] bytes = compressJpeg(current, quality);
            if (bytes.length <= SCREENSHOT_MAX_ENCODED_BYTES || current.getWidth() <= 360 || current.getHeight() <= 360) {
                return new EncodedScreenshot(current, currentOwned, bytes, quality, downscaled);
            }

            if (quality > SCREENSHOT_MIN_JPEG_QUALITY) {
                quality = Math.max(SCREENSHOT_MIN_JPEG_QUALITY, quality - 10);
                continue;
            }

            Bitmap next = Bitmap.createScaledBitmap(
                    current,
                    Math.max(1, Math.round(current.getWidth() * SCREENSHOT_DOWNSCALE_STEP)),
                    Math.max(1, Math.round(current.getHeight() * SCREENSHOT_DOWNSCALE_STEP)),
                    true
            );
            if (currentOwned) current.recycle();
            current = next;
            currentOwned = true;
            downscaled = true;
            quality = SCREENSHOT_INITIAL_JPEG_QUALITY;
        }
    }

    private byte[] compressJpeg(Bitmap bitmap, int quality) {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        bitmap.compress(Bitmap.CompressFormat.JPEG, quality, output);
        return output.toByteArray();
    }

    private static final class EncodedScreenshot {
        final Bitmap bitmap;
        final boolean bitmapOwned;
        final byte[] bytes;
        final int quality;
        final boolean downscaled;

        EncodedScreenshot(Bitmap bitmap, boolean bitmapOwned, byte[] bytes, int quality, boolean downscaled) {
            this.bitmap = bitmap;
            this.bitmapOwned = bitmapOwned;
            this.bytes = bytes;
            this.quality = quality;
            this.downscaled = downscaled;
        }

        void recycle() {
            if (bitmapOwned) bitmap.recycle();
        }
    }

    private boolean dispatch(Path path, long startDelayMs, long durationMs) throws Exception {
        CountDownLatch latch = new CountDownLatch(1);
        AtomicBoolean completed = new AtomicBoolean(false);
        GestureDescription gesture = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(path, startDelayMs, durationMs))
                .build();

        boolean accepted = dispatchGesture(gesture, new GestureResultCallback() {
            @Override
            public void onCompleted(GestureDescription gestureDescription) {
                completed.set(true);
                latch.countDown();
            }

            @Override
            public void onCancelled(GestureDescription gestureDescription) {
                completed.set(false);
                latch.countDown();
            }
        }, null);

        if (!accepted) return false;
        latch.await(Math.max(1500, durationMs + 1500), TimeUnit.MILLISECONDS);
        return completed.get();
    }

    private JSONObject nodeJson(AccessibilityNodeInfo node, int depth) throws Exception {
        JSONObject output = JsonUtil.obj(
                "class_name", safe(node.getClassName()),
                "package_name", safe(node.getPackageName()),
                "text", safe(node.getText()),
                "content_description", safe(node.getContentDescription()),
                "view_id", safe(node.getViewIdResourceName()),
                "clickable", node.isClickable(),
                "long_clickable", node.isLongClickable(),
                "editable", node.isEditable(),
                "enabled", node.isEnabled(),
                "password", node.isPassword(),
                "focused", node.isFocused(),
                "selected", node.isSelected(),
                "scrollable", node.isScrollable()
        );
        Rect bounds = new Rect();
        node.getBoundsInScreen(bounds);
        output.put("bounds", rectJson(bounds));

        JSONArray children = new JSONArray();
        if (depth < 8) {
            for (int i = 0; i < node.getChildCount(); i++) {
                AccessibilityNodeInfo child = node.getChild(i);
                if (child == null) continue;
                try {
                    children.put(nodeJson(child, depth + 1));
                } finally {
                    child.recycle();
                }
            }
        }
        output.put("children", children);
        return output;
    }

    JSONObject describeNodeAt(float x, float y) throws Exception {
        NodeMatch match = new NodeMatch();
        if (!isHuaweiBuild() && Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            List<android.view.accessibility.AccessibilityWindowInfo> windows = getWindows();
            for (android.view.accessibility.AccessibilityWindowInfo window : windows) {
                AccessibilityNodeInfo root = window.getRoot();
                if (root == null) continue;
                try {
                    findNodeAt(root, (int) x, (int) y, 0, match);
                } finally {
                    root.recycle();
                }
            }
        }

        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root != null) {
            try {
                findNodeAt(root, (int) x, (int) y, 0, match);
            } finally {
                root.recycle();
            }
        }

        if (match.budget.isTruncated()) {
            return JsonUtil.obj("found", false, "x", x, "y", y,
                    "package_name", lastPackageName, "observation_error", "target_traversal_truncated");
        }
        return match.node == null
                ? JsonUtil.obj("found", false, "x", x, "y", y, "package_name", lastPackageName)
                : match.node.put("found", true).put("x", x).put("y", y);
    }

    JSONObject focusedInputSummary() throws Exception {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return JsonUtil.obj("found", false);
        try {
            AccessibilityNodeInfo focused = findFocusedEditableNode(root);
            if (focused == null) return JsonUtil.obj("found", false);
            try {
                return nodeSummary(focused).put("found", true);
            } finally {
                focused.recycle();
            }
        } finally {
            root.recycle();
        }
    }

    private AccessibilityNodeInfo findFocusedEditableNode(AccessibilityNodeInfo root) {
        AccessibilityNodeInfo direct = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        if (isFocusedEditableNode(direct)) return direct;
        if (direct != null) direct.recycle();

        FocusedInputMatch match = new FocusedInputMatch();
        collectFocusedEditableNodes(root, 0, match);
        return match.node;
    }

    private void collectFocusedEditableNodes(AccessibilityNodeInfo node, int depth, FocusedInputMatch match) {
        if (node == null || depth > 20) return;
        if (isFocusedEditableNode(node)) {
            Rect bounds = new Rect();
            node.getBoundsInScreen(bounds);
            int area = Math.max(1, bounds.width() * bounds.height());
            boolean hasSemantics = hasNodeActionSemantics(node);
            if (PolicyGate.preferTapTarget(
                    match.node != null,
                    match.hasSemantics,
                    match.depth,
                    match.area,
                    hasSemantics,
                    depth,
                    area)) {
                if (match.node != null) match.node.recycle();
                match.node = AccessibilityNodeInfo.obtain(node);
                match.hasSemantics = hasSemantics;
                match.depth = depth;
                match.area = area;
            }
        }

        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child == null) continue;
            try {
                collectFocusedEditableNodes(child, depth + 1, match);
            } finally {
                child.recycle();
            }
        }
    }

    private boolean isFocusedEditableNode(AccessibilityNodeInfo node) {
        return node != null && node.isFocused() && node.isEditable();
    }

    JSONObject policyScreenSummary() throws Exception {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) {
            return JsonUtil.obj("found", false, "package_name", lastPackageName, "screen_text", "");
        }
        try {
            StringBuilder text = new StringBuilder();
            PolicySignals signals = new PolicySignals();
            collectPolicySignals(root, text, signals, 0);
            String rootPackageName = root.getPackageName() == null
                    ? ""
                    : root.getPackageName().toString();
            String screenPackageName = PolicyGate.authoritativeScreenPackage(
                    rootPackageName,
                    lastPackageName
            );
            return JsonUtil.obj(
                    "found", true,
                    "package_name", screenPackageName,
                    "screen_text", text.toString(),
                    "screen_has_semantics", signals.hasSemantics,
                    "financial_risk_detected", signals.financialRisk,
                    "call_sms_risk_detected", signals.callSmsRisk,
                    "protected_package", PolicyGate.isProtectedMutationPackage(screenPackageName)
            );
        } finally {
            root.recycle();
        }
    }

    private void collectPolicySignals(AccessibilityNodeInfo node, StringBuilder output, PolicySignals signals, int depth) {
        if (depth > 20) return;
        inspectPolicyValue(output, signals, node.getText(), true);
        inspectPolicyValue(output, signals, node.getContentDescription(), true);
        inspectPolicyValue(output, signals, node.getViewIdResourceName(), true);
        inspectPolicyValue(output, signals, node.getPackageName(), false);
        inspectPolicyValue(output, signals, node.getClassName(), false);
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child == null) continue;
            try {
                collectPolicySignals(child, output, signals, depth + 1);
            } finally {
                child.recycle();
            }
        }
    }

    private void inspectPolicyValue(StringBuilder output, PolicySignals signals, CharSequence value, boolean semantic) {
        if (value == null || value.length() == 0) return;
        String clean = value.toString().replace('\n', ' ').replace('\r', ' ');
        if (semantic) signals.hasSemantics = true;
        if (PolicyGate.containsFinancialRiskTerm(clean)) signals.financialRisk = true;
        if (PolicyGate.containsCallSmsAuthorizationTerm(clean)) signals.callSmsRisk = true;
        appendPolicyValue(output, clean);
    }

    private void appendPolicyValue(StringBuilder output, CharSequence value) {
        if (value == null || value.length() == 0 || output.length() >= 16384) return;
        int remaining = 16384 - output.length();
        String clean = value.toString().replace('\n', ' ').replace('\r', ' ');
        if (clean.length() > remaining) clean = clean.substring(0, remaining);
        output.append(clean).append('\n');
    }

    private static final class PolicySignals {
        boolean hasSemantics;
        boolean financialRisk;
        boolean callSmsRisk;
    }

    private void findNodeAt(AccessibilityNodeInfo node, int x, int y, int depth, NodeMatch match) throws Exception {
        if (!match.budget.visit(depth)) return;
        Rect bounds = new Rect();
        node.getBoundsInScreen(bounds);
        if (!bounds.contains(x, y)) return;

        int area = Math.max(1, bounds.width() * bounds.height());
        boolean hasSemantics = hasNodeActionSemantics(node);
        if (PolicyGate.preferTapTarget(
                match.node != null,
                match.hasSemantics,
                match.depth,
                match.area,
                hasSemantics,
                depth,
                area)) {
            match.node = nodeSummary(node);
            match.area = area;
            match.depth = depth;
            match.hasSemantics = hasSemantics;
        }

        for (int i = 0; i < node.getChildCount(); i++) {
            if (match.budget.isTruncated()) break;
            AccessibilityNodeInfo child = node.getChild(i);
            if (child == null) continue;
            try {
                findNodeAt(child, x, y, depth + 1, match);
            } finally {
                child.recycle();
            }
        }
    }

    private JSONObject nodeSummary(AccessibilityNodeInfo node) throws Exception {
        Rect bounds = new Rect();
        node.getBoundsInScreen(bounds);
        return JsonUtil.obj(
                "class_name", safe(node.getClassName()),
                "package_name", safe(node.getPackageName()),
                "text", safe(node.getText()),
                "content_description", safe(node.getContentDescription()),
                "view_id", safe(node.getViewIdResourceName()),
                "clickable", node.isClickable(),
                "editable", node.isEditable(),
                "enabled", node.isEnabled(),
                "focused", node.isFocused(),
                "password", node.isPassword(),
                "bounds", rectJson(bounds)
        );
    }

    private boolean hasNodeActionSemantics(AccessibilityNodeInfo node) {
        return node != null && (
                !safe(node.getText()).trim().isEmpty()
                        || !safe(node.getContentDescription()).trim().isEmpty()
                        || !safe(node.getViewIdResourceName()).trim().isEmpty());
    }

    private JSONObject rectJson(Rect rect) {
        return JsonUtil.obj(
                "left", rect.left,
                "top", rect.top,
                "right", rect.right,
                "bottom", rect.bottom,
                "width", rect.width(),
                "height", rect.height()
        );
    }

    private String safe(CharSequence value) {
        return value == null ? "" : value.toString();
    }

    private boolean isHuaweiBuild() {
        String manufacturer = Build.MANUFACTURER == null ? "" : Build.MANUFACTURER.toLowerCase(java.util.Locale.US);
        String brand = Build.BRAND == null ? "" : Build.BRAND.toLowerCase(java.util.Locale.US);
        return manufacturer.contains("huawei") || brand.contains("huawei") || brand.contains("honor");
    }

    private static final class NodeMatch {
        final TapTargetBudget budget = new TapTargetBudget();
        JSONObject node;
        int area = Integer.MAX_VALUE;
        int depth = -1;
        boolean hasSemantics;
    }

    private static final class FocusedInputMatch {
        AccessibilityNodeInfo node;
        int area = Integer.MAX_VALUE;
        int depth = -1;
        boolean hasSemantics;
    }
}
