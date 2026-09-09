package com.chebysight.chebyagent.android;

import android.content.Context;
import android.content.Intent;
import android.content.ComponentName;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.util.DisplayMetrics;
import android.view.WindowManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.atomic.AtomicBoolean;

final class AgentNodeSdk {
    private static final AtomicBoolean TARGET_LAUNCH_IN_PROGRESS = new AtomicBoolean();
    private final Context context;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    AgentNodeSdk(Context context) {
        this.context = context.getApplicationContext();
    }

    JSONObject status() throws Exception {
        DisplayMetrics metrics = new DisplayMetrics();
        WindowManager windowManager = (WindowManager) context.getSystemService(Context.WINDOW_SERVICE);
        if (windowManager != null) {
            windowManager.getDefaultDisplay().getRealMetrics(metrics);
        }

        AgentAccessibilityService service = AgentAccessibilityService.getInstance();
        JSONObject accessibilityRuntime = service == null
                ? JsonUtil.obj("bound", false)
                : service.runtimeStatus();

        JSONObject output = JsonUtil.obj(
                "ok", true,
                "device_kind", "android",
                "package_name", context.getPackageName(),
                "product_name", BuildConfig.AGENT_NODE_PRODUCT_NAME,
                "phonebridge_enabled", BuildConfig.PHONEBRIDGE_ENABLED,
                "agent_node_running", AgentNodeService.isRunning(),
                "agent_node_paused", AgentNodeService.isPaused(),
                "dispatch_mode", "direct_with_checkout_consent",
                "action_constraints_enabled", false,
                "fund_protection_enabled", false,
                "checkout_preapproved_by_owner", CommerceAuthorization.isPreapproved(context),
                "call_sms_allowed", true,
                "browser_launch_allowed", true,
                "checkout_confirmation_required", !CommerceAuthorization.isPreapproved(context),
                "accessibility_enabled", AccessibilityState.isServiceEnabled(context),
                "accessibility_bound", service != null,
                "accessibility_runtime", accessibilityRuntime,
                "surface_state", AgentSurfaceState.snapshot(context, AccessibilityState.isServiceEnabled(context) && service != null),
                "screen_width", metrics.widthPixels,
                "screen_height", metrics.heightPixels,
                "density", metrics.densityDpi,
                "message_for_user", service == null
                        ? BuildConfig.AGENT_NODE_PRODUCT_NAME + " is running, but Accessibility is not bound yet."
                        : BuildConfig.AGENT_NODE_PRODUCT_NAME + " Android PhoneNode is live."
        );
        output.put("runtime_role", "phone_device_executor");
        output.put("phonebridge", PhoneBridgeClient.snapshot());
        output.put("cloud_gateway", PhoneBridgeClient.snapshot());
        return output;
    }

    JSONObject capabilities() throws Exception {
        JSONArray actions = JsonUtil.arr(
                action("observe", "Returns fast phone state without full UI tree capture."),
                action("execute", "Dispatches a normalized phone action directly."),
                action("control", "Control the local AgentNode runtime."),
                action("status", "Returns the PhoneBridge device executor status."),
                action("capabilities", "Lists available PhoneNode actions."),
                action("ui_tree", "Requires this app's Accessibility service."),
                action("capture_screenshot", "Requires Android Accessibility screenshot permission."),
                action("surface_state", "Returns the user-facing Agent Surface state."),
                action("set_surface_state", "Updates user-facing Agent Surface state."),
                action("pause_agent", "Records pause intent without blocking actions."),
                action("resume_agent", "Keeps direct action dispatch enabled."),
                action("stop_agent", "Stop the local AgentNode service."),
                action("tap_screen", "Requires this app's Accessibility service."),
                action("swipe_screen", "Requires this app's Accessibility service."),
                action("input_text", "Sets text on the focused editable node."),
                action("press_back", "Accessibility global action."),
                action("press_home", "Accessibility global action."),
                action("open_app", "Launches an installed app package."),
                action("open_url", "Opens a URL using an installed handler.")
        );
        return JsonUtil.obj(
                "actions", actions,
                "dispatch", JsonUtil.obj(
                        "mode", "direct_with_checkout_consent",
                        "interception_enabled", false,
                        "fund_protection_enabled", false,
                        "checkout_preapproved_by_owner", CommerceAuthorization.isPreapproved(context),
                        "call_sms_allowed", true,
                "browser_launch_allowed", true,
                "checkout_confirmation_required", !CommerceAuthorization.isPreapproved(context)
                )
        );
    }

    JSONObject observe() throws Exception {
        JSONObject status = status();
        JSONObject output = JsonUtil.obj(
                "ok", true,
                "status", status,
                "active_package", "",
                "ui_tree_available", false,
                "ui_tree_route", "phone.ui_tree"
        );
        JSONObject runtime = status.optJSONObject("accessibility_runtime");
        if (runtime != null) {
            output.put("active_package", runtime.optString("last_package_name", ""));
        }
        AuditLog.append(context, "sdk_observe", JsonUtil.obj(
                "active_package", output.optString("active_package"),
                "ui_tree_available", output.optBoolean("ui_tree_available")
        ));
        return output;
    }

    JSONObject execute(JSONObject action) throws Exception {
        JSONObject payload = action == null ? new JSONObject() : action;
        String tool = firstNonEmpty(
                payload.optString("tool", ""),
                payload.optString("name", ""),
                payload.optString("type", ""),
                payload.optString("action_type", "")
        );
        JSONObject nestedAction = payload.optJSONObject("action");
        if (tool.isEmpty() && nestedAction != null) {
            tool = firstNonEmpty(
                    nestedAction.optString("tool", ""),
                    nestedAction.optString("name", ""),
                    nestedAction.optString("type", ""),
                    nestedAction.optString("action_type", "")
            );
        }

        JSONObject args = payload.optJSONObject("arguments");
        if (args == null) args = payload.optJSONObject("args");
        if (args == null) args = payload.optJSONObject("params");
        if (args == null) args = nestedAction;
        if (args == null) args = payload;

        String normalized = normalizeToolName(tool);
        if ("execute".equals(normalized)) return execute(args);
        if ("control".equals(normalized)) return control(args);
        return executeTool(normalized, args);
    }

    JSONObject control(JSONObject input) throws Exception {
        JSONObject payload = input == null ? new JSONObject() : input;
        return control(firstNonEmpty(
                payload.optString("action", ""),
                payload.optString("type", ""),
                payload.optString("command", "")
        ));
    }

    JSONObject control(String action) throws Exception {
        String normalized = action == null ? "" : action.trim().toLowerCase(Locale.US).replace("-", "_");
        if ("pause".equals(normalized) || "pause_agent".equals(normalized)) return pauseAgent();
        if ("resume".equals(normalized) || "resume_agent".equals(normalized)) return resumeAgent();
        if ("stop".equals(normalized) || "stop_agent".equals(normalized)) return stopAgent();
        if ("status".equals(normalized) || normalized.isEmpty()) return status();
        throw new IllegalArgumentException("Unsupported control action: " + action);
    }

    JSONObject executeTool(String name, JSONObject args) throws Exception {
        String normalized = normalizeToolName(name);
        JSONObject payload = args == null ? new JSONObject() : args;
        switch (normalized) {
            case "status":
                return status();
            case "capabilities":
                return capabilities();
            case "observe":
                return observe();
            case "ui_tree":
                return uiTree();
            case "screenshot":
                if ("transport".equals(payload.optString("mode", ""))) return transportScreenshot();
                return screenshot();
            case "surface_state":
                return surfaceState();
            case "set_surface_state":
                return setSurfaceState(payload);
            case "clear_surface_state":
                return clearSurfaceState();
            case "pause_agent":
                return pauseAgent();
            case "resume_agent":
                return resumeAgent();
            case "stop_agent":
                return stopAgent();
            case "tap_screen":
                return tap((float) payload.optDouble("x"), (float) payload.optDouble("y"));
            case "swipe_screen":
                return swipe(
                        (float) payload.optDouble("start_x"),
                        (float) payload.optDouble("start_y"),
                        (float) payload.optDouble("end_x"),
                        (float) payload.optDouble("end_y"),
                        payload.optLong("duration_ms", 350)
                );
            case "input_text":
                return inputText(payload.optString("text", ""));
            case "press_back":
                return pressBack();
            case "press_home":
                return pressHome();
            case "press_recents":
                return pressRecents();
            case "open_app":
                return openApp(firstNonEmpty(payload.optString("package_name", ""), payload.optString("packageName", "")));
            default:
                throw new IllegalArgumentException("Unknown AgentNode SDK action: " + name);
        }
    }

    JSONObject uiTree() throws Exception {
        AgentAccessibilityService service = requireAccessibility();
        return service.captureUiTree().put("overlay_hidden_for_observation", false);
    }

    JSONObject screenshot() throws Exception {
        AgentAccessibilityService service = requireAccessibility();
        JSONObject screenshot = service.takePhoneBridgeScreenshot();
        screenshot.put("overlay_hidden_for_observation", false);
        screenshot.put("capture_method", "accessibility_take_screenshot");
        AuditLog.append(context, "capture_screenshot", JsonUtil.obj(
                "width", screenshot.optInt("width"),
                "height", screenshot.optInt("height"),
                "screen_width", screenshot.optInt("screen_width"),
                "screen_height", screenshot.optInt("screen_height"),
                "encoded_bytes", screenshot.optInt("encoded_bytes"),
                "overlay_hidden_for_observation", false
        ));
        return screenshot;
    }

    JSONObject transportScreenshot() throws Exception {
        AgentAccessibilityService service = requireAccessibility();
        JSONObject screenshot = service.takeTransportScreenshot();
        screenshot.put("overlay_hidden_for_observation", false);
        screenshot.put("capture_method", "accessibility_take_screenshot");
        AuditLog.append(context, "capture_screenshot", JsonUtil.obj(
                "width", screenshot.optInt("width"),
                "height", screenshot.optInt("height"),
                "screen_width", screenshot.optInt("screen_width"),
                "screen_height", screenshot.optInt("screen_height"),
                "encoded_bytes", screenshot.optInt("encoded_bytes"),
                "overlay_hidden_for_observation", false,
                "transport_compat", true
        ));
        return screenshot;
    }

    JSONObject surfaceState() {
        return AgentSurfaceState.snapshot(context, AccessibilityState.isServiceEnabled(context) && AgentAccessibilityService.getInstance() != null);
    }

    JSONObject setSurfaceState(JSONObject input) throws Exception {
        JSONObject state = AgentSurfaceState.update(context, input);
        AuditLog.append(context, "surface_state_updated", JsonUtil.obj(
                "phase", state.optString("phase"),
                "has_task", !state.optString("task").isEmpty(),
                "has_result", !state.optString("result").isEmpty()
        ));
        return state;
    }

    JSONObject clearSurfaceState() throws Exception {
        JSONObject state = AgentSurfaceState.clear(context);
        AuditLog.append(context, "surface_state_cleared", JsonUtil.obj("ok", true));
        return state;
    }

    JSONObject pauseAgent() throws Exception {
        AgentNodeService.setPaused(false);
        AgentSurfaceState.setPhase(context, AgentSurfaceState.RUNNING, "Passthrough dispatch remains enabled.", "");
        AuditLog.append(context, "pause_agent_passthrough", JsonUtil.obj("ok", true));
        return JsonUtil.obj("ok", true, "status", "running", "message_for_user", BuildConfig.AGENT_NODE_PRODUCT_NAME + " passthrough dispatch remains enabled.");
    }

    JSONObject resumeAgent() throws Exception {
        AgentNodeService.setPaused(false);
        AgentSurfaceState.setPhase(context, AgentSurfaceState.RUNNING, "", "");
        AuditLog.append(context, "resume_agent", JsonUtil.obj("ok", true));
        return JsonUtil.obj("ok", true, "status", "running", "message_for_user", BuildConfig.AGENT_NODE_PRODUCT_NAME + " is running.");
    }

    JSONObject stopAgent() throws Exception {
        AgentNodeService.setPaused(false);
        AgentSurfaceState.clear(context);
        AuditLog.append(context, "stop_agent", JsonUtil.obj("ok", true));
        mainHandler.postDelayed(() -> context.stopService(new Intent(context, AgentNodeService.class)), 150);
        return JsonUtil.obj("ok", true, "status", "stopping", "message_for_user", BuildConfig.AGENT_NODE_PRODUCT_NAME + " is stopping.");
    }

    JSONObject tap(float x, float y) throws Exception {
        AgentAccessibilityService service = requireAccessibility();
        JSONObject policy = PolicyGate.evaluateTap(service, x, y);
        if (!policy.optBoolean("allowed", false)) {
            if (policy.optBoolean("confirmation_required", false)) {
                CommerceAuthorization.Pending request = CommerceAuthorization.request(policy.getJSONObject("evidence"));
                Intent consent = new Intent(context, CommerceAuthorizationActivity.class)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                        .putExtra(CommerceAuthorizationActivity.EXTRA_REQUEST, request.id);
                mainHandler.post(() -> {
                    try {
                        context.startActivity(consent);
                    } catch (RuntimeException ignored) {
                        // The action remains unexecuted if Android prevents foreground handoff.
                    }
                });
            }
            return policy;
        }
        boolean ok = service.tap(x, y);
        JSONObject details = JsonUtil.obj("x", x, "y", y, "ok", ok,
                "authorization_source", policy.optString("authorization_source", "ordinary_action"));
        AuditLog.append(context, "tap_screen", details);
        return JsonUtil.obj("ok", ok, "x", x, "y", y, "message_for_user", ok ? "Tap dispatched." : "Tap was not accepted.");
    }

    JSONObject swipe(float startX, float startY, float endX, float endY, long durationMs) throws Exception {
        AgentAccessibilityService service = requireAccessibility();
        JSONObject policy = PolicyGate.evaluateSwipe(service);
        if (!policy.optBoolean("allowed", false)) return policy;
        boolean ok = service.swipe(startX, startY, endX, endY, durationMs);
        JSONObject details = JsonUtil.obj(
                "start_x", startX,
                "start_y", startY,
                "end_x", endX,
                "end_y", endY,
                "duration_ms", durationMs,
                "ok", ok
        );
        AuditLog.append(context, "swipe_screen", details);
        return JsonUtil.obj("ok", ok, "message_for_user", ok ? "Swipe dispatched." : "Swipe was not accepted.");
    }

    JSONObject inputText(String text) throws Exception {
        AgentAccessibilityService service = requireAccessibility();
        JSONObject policy = PolicyGate.evaluateInputText(service);
        if (!policy.optBoolean("allowed", false)) return policy;
        boolean ok = service.inputText(text == null ? "" : text);
        AuditLog.append(context, "input_text", JsonUtil.obj("ok", ok, "length", text == null ? 0 : text.length()));
        return JsonUtil.obj("ok", ok, "message_for_user", ok ? "Text set on focused input." : "No focused editable input accepted text.");
    }

    JSONObject pressBack() throws Exception {
        boolean ok = requireAccessibility().pressBack();
        AuditLog.append(context, "press_back", JsonUtil.obj("ok", ok));
        return JsonUtil.obj("ok", ok, "message_for_user", ok ? "Back pressed." : "Back action was not accepted.");
    }

    JSONObject pressHome() throws Exception {
        boolean ok = requireAccessibility().pressHome();
        AuditLog.append(context, "press_home", JsonUtil.obj("ok", ok));
        return JsonUtil.obj("ok", ok, "message_for_user", ok ? "Home pressed." : "Home action was not accepted.");
    }

    JSONObject pressRecents() throws Exception {
        boolean ok = requireAccessibility().pressRecents();
        AuditLog.append(context, "press_recents", JsonUtil.obj("ok", ok));
        return JsonUtil.obj("ok", ok, "message_for_user", ok ? "Recents opened." : "Recents action was not accepted.");
    }

    JSONObject openUrl(String url) throws Exception {
        if (url == null || url.trim().isEmpty()) {
            throw new IllegalArgumentException("url is required.");
        }
        Uri uri = Uri.parse(url.trim());
        JSONObject policy = PolicyGate.evaluateOpenUrl(context, uri);
        if (!policy.optBoolean("allowed", false)) return policy;
        Intent intent = new Intent(Intent.ACTION_VIEW, uri);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        ComponentName resolved = resolveUrlIntent(intent);
        if (resolved == null) throw new IllegalArgumentException("No installed app can open this URL.");
        JSONObject auditUrl = urlAuditSummary(uri);
        AuditLog.append(context, "open_url", JsonUtil.obj("url", auditUrl));
        JSONObject launch = launchAndVerify(
                "open_url",
                intent,
                JsonUtil.obj("url", auditUrl),
                resolved.getPackageName()
        );
        launch.put("url", uri.toString());
        boolean selectionRequired = isResolverComponent(
                resolved.getPackageName(),
                resolved.getClassName()
        );
        launch.put("selection_required", selectionRequired);
        launch.put(
                "message_for_user",
                selectionRequired
                        ? "Browser selection is required before the URL is open."
                        : "URL opened and foreground verified."
        );
        return launch;
    }

    JSONObject openApp(String packageName) throws Exception {
        if (packageName == null || packageName.trim().isEmpty()) {
            throw new IllegalArgumentException("package_name is required.");
        }
        String trimmedPackageName = packageName.trim();
        JSONObject policy = PolicyGate.evaluateOpenApp(trimmedPackageName);
        if (!policy.optBoolean("allowed", false)) return policy;
        PackageManager packageManager = context.getPackageManager();
        packageManager.getPackageInfo(trimmedPackageName, 0);

        Intent intent;
        if (isSystemSettingsPackage(trimmedPackageName)) {
            // Settings has no reliable launcher activity on several Huawei
            // builds.  The platform Settings action is exported and stable.
            intent = new Intent(Settings.ACTION_SETTINGS);
            intent.setPackage(trimmedPackageName);
        } else {
            intent = packageManager.getLaunchIntentForPackage(trimmedPackageName);
        }
        if (intent == null) {
            intent = new Intent(Intent.ACTION_MAIN);
            intent.addCategory(Intent.CATEGORY_LAUNCHER);
            intent.setPackage(trimmedPackageName);
        }
        if (intent.resolveActivity(packageManager) == null) {
            throw new IllegalArgumentException("Package has no launchable activity.");
        }
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        intent.addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT | Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        AuditLog.append(context, "open_app", JsonUtil.obj("package_name", trimmedPackageName));
        JSONObject launch = launchAndVerify(
                "open_app",
                intent,
                JsonUtil.obj("package_name", trimmedPackageName),
                trimmedPackageName
        );
        launch.put("package_name", trimmedPackageName);
        launch.put("message_for_user", "App opened and foreground verified.");
        return launch;
    }

    static boolean isSystemSettingsPackage(String packageName) {
        return "com.android.settings".equals(packageName);
    }

    private ComponentName resolveUrlIntent(Intent intent) {
        PackageManager packageManager = context.getPackageManager();
        List<ResolveInfo> candidates = packageManager.queryIntentActivities(intent, 0);
        Set<String> packages = new HashSet<>();
        for (ResolveInfo candidate : candidates) {
            if (candidate.activityInfo != null
                    && candidate.activityInfo.packageName != null
                    && !candidate.activityInfo.packageName.trim().isEmpty()) {
                packages.add(candidate.activityInfo.packageName);
            }
        }
        String onlyPackage = singleDistinctPackage(packages);
        if (!onlyPackage.isEmpty()) intent.setPackage(onlyPackage);
        return intent.resolveActivity(packageManager);
    }

    static String singleDistinctPackage(Set<String> packages) {
        if (packages == null || packages.size() != 1) return "";
        String value = packages.iterator().next();
        return value == null ? "" : value;
    }

    static boolean isResolverComponent(String packageName, String className) {
        return (className != null && className.contains("ResolverActivity"))
                || (packageName != null && packageName.contains(".internal.app"));
    }

    static boolean tryBeginTargetLaunch() {
        return TARGET_LAUNCH_IN_PROGRESS.compareAndSet(false, true);
    }

    static void endTargetLaunch() {
        TARGET_LAUNCH_IN_PROGRESS.set(false);
    }

    private String normalizeToolName(String name) {
        String value = name == null ? "" : name.trim();
        if (value.startsWith("phone.")) value = value.substring("phone.".length());
        if ("phone_status".equals(value)) return "status";
        if ("phone_agent_runtime_status".equals(value)) return "agent_runtime_status";
        if ("phone_codex_runtime_status".equals(value)) return "codex_runtime_status";
        if ("phone_codex_runtime_doctor".equals(value)) return "codex_runtime_doctor";
        if ("phone_codex_runtime_bootstrap".equals(value)) return "codex_runtime_bootstrap";
        if ("phone_codex_runtime_version".equals(value)) return "codex_runtime_version";
        if ("phone_codex_runtime_login_status".equals(value)) return "codex_runtime_login_status";
        if ("phone_codex_runtime_login_start".equals(value)) return "codex_runtime_login_start";
        if ("phone_codex_runtime_exec".equals(value)) return "codex_runtime_exec";
        if ("phone_codex_runtime_sidecar_start".equals(value)) return "codex_runtime_sidecar_start";
        if ("phone_codex_runtime_sidecar_status".equals(value)) return "codex_runtime_sidecar_status";
        if ("phone_hermes_runtime_status".equals(value)) return "hermes_runtime_status";
        if ("phone_hermes_runtime_doctor".equals(value)) return "hermes_runtime_doctor";
        if ("phone_hermes_runtime_bootstrap".equals(value)) return "hermes_runtime_bootstrap";
        if ("phone_hermes_runtime_version".equals(value)) return "hermes_runtime_version";
        if ("phone_capabilities".equals(value)) return "capabilities";
        if ("phone_observe".equals(value)) return "observe";
        if ("phone_ui_tree".equals(value)) return "ui_tree";
        if ("phone_screenshot".equals(value) || "capture_screenshot".equals(value)) return "screenshot";
        if ("phone_execute".equals(value)) return "execute";
        if ("phone_control".equals(value)) return "control";
        if ("phone_open_app".equals(value)) return "open_app";
        if ("phone_open_url".equals(value)) return "open_url";
        if ("phone_tap".equals(value)) return "tap_screen";
        if ("phone_swipe".equals(value)) return "swipe_screen";
        if ("phone_input_text".equals(value)) return "input_text";
        if ("phone_back".equals(value)) return "press_back";
        if ("phone_home".equals(value)) return "press_home";
        if ("phone_recents".equals(value)) return "press_recents";
        if ("screenshot".equals(value)) return "screenshot";
        return value;
    }

    private String firstNonEmpty(String... values) {
        if (values == null) return "";
        for (String value : values) {
            if (value != null && !value.trim().isEmpty()) return value.trim();
        }
        return "";
    }

    private String userError(Exception error) {
        String message = error.getMessage();
        return message == null || message.trim().isEmpty() ? String.valueOf(error) : message;
    }

    private JSONObject launchAndVerify(
            String action,
            Intent intent,
            JSONObject details,
            String expectedPackage
    ) throws Exception {
        if (!tryBeginTargetLaunch()) {
            throw new IllegalStateException("Another target launch is already in progress.");
        }
        PendingTargetLaunch.Request pending = null;
        try {
            AgentAccessibilityService service = requireAccessibility();
            try {
                context.startActivity(intent);
                AuditLog.append(context, action + "_launch_attempted", JsonUtil.obj(
                        "details", details,
                        "path", "application_context"
                ));
                if (service.awaitForegroundPackage(expectedPackage, 1200L)) {
                    return launchSuccess(action, details, expectedPackage, "application_context", "");
                }
            } catch (Exception directError) {
                AuditLog.append(context, action + "_direct_failed", JsonUtil.obj(
                        "details", details,
                        "error", userError(directError)
                ));
            }

            pending = PendingTargetLaunch.begin(intent, action);
            if (!service.openOwnLauncherActivity(3500L)) {
                throw new IllegalStateException("ChebyNode launcher icon could not be activated.");
            }
            if (!pending.await(4500L)) {
                String suffix = pending.error().isEmpty() ? "" : " " + pending.error();
                throw new IllegalStateException("ChebyNode foreground trampoline did not dispatch the target." + suffix);
            }
            if (!service.awaitForegroundPackage(expectedPackage, 5000L)) {
                throw new IllegalStateException("Target app did not become the verified foreground package.");
            }
            return launchSuccess(action, details, expectedPackage, "foreground_trampoline", pending.requestId());
        } catch (Exception error) {
            AuditLog.append(context, action + "_failed", JsonUtil.obj(
                    "details", details,
                    "expected_package", expectedPackage,
                    "error", userError(error)
            ));
            throw error;
        } finally {
            PendingTargetLaunch.cancel(pending, "Target launch scope ended.");
            endTargetLaunch();
        }
    }

    private JSONObject launchSuccess(
            String action,
            JSONObject details,
            String expectedPackage,
            String path,
            String requestId
    ) {
        AuditLog.append(context, action + "_verified", JsonUtil.obj(
                "details", details,
                "expected_package", expectedPackage,
                "path", path,
                "request_id", requestId
        ));
        return JsonUtil.obj(
                "ok", true,
                "foreground_package", expectedPackage,
                "launch_path", path,
                "request_id", requestId
        );
    }

    private JSONObject urlAuditSummary(Uri uri) {
        return JsonUtil.obj(
                "scheme", uri.getScheme() == null ? "" : uri.getScheme(),
                "host", uri.getHost() == null ? "" : uri.getHost(),
                "path", uri.getPath() == null ? "" : uri.getPath(),
                "has_query", uri.getQuery() != null && !uri.getQuery().isEmpty(),
                "has_fragment", uri.getFragment() != null && !uri.getFragment().isEmpty()
        );
    }

    private AgentAccessibilityService requireAccessibility() {
        AgentAccessibilityService service = AgentAccessibilityService.getInstance();
        if (service == null) {
            throw new IllegalStateException(BuildConfig.AGENT_NODE_PRODUCT_NAME + " Accessibility service is not bound. Open Android Accessibility settings and enable " + BuildConfig.AGENT_NODE_PRODUCT_NAME + " Control.");
        }
        return service;
    }

    private JSONObject action(String name, String availability) {
        return JsonUtil.obj(
            "action", name,
            "mode", "direct_with_checkout_consent",
            "availability", availability
        );
    }
}
