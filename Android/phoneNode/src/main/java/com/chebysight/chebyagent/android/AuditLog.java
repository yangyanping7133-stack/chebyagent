package com.chebysight.chebyagent.android;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.FileWriter;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.Locale;

final class AuditLog {
    private static final String FILE_NAME = "audit.jsonl";
    private static final int MAX_STORED_EVENTS = 80;
    private static final int DEFAULT_RECENT_LIMIT = 20;
    private static final int MAX_TEXT_LENGTH = 420;

    private AuditLog() {
    }

    static synchronized void append(Context context, String type, JSONObject details) {
        try {
            JSONObject event = new JSONObject();
            event.put("type", sanitizeText(type == null ? "event" : type));
            event.put("created_at", System.currentTimeMillis());
            event.put("details", sanitizeJson(details == null ? new JSONObject() : details));

            File file = file(context);
            List<JSONObject> events = readEvents(file);
            events.add(event);
            writeTail(file, events, MAX_STORED_EVENTS);
        } catch (Exception ignored) {
            // Audit must never crash the accessibility or transport path.
        }
    }

    static synchronized JSONArray recent(Context context) {
        return recent(context, DEFAULT_RECENT_LIMIT);
    }

    static synchronized JSONArray recent(Context context, int limit) {
        JSONArray output = new JSONArray();
        try {
            List<JSONObject> events = readEvents(file(context));
            int count = 0;
            for (int i = events.size() - 1; i >= 0 && count < limit; i--, count++) {
                output.put(events.get(i));
            }
        } catch (Exception ignored) {
        }
        return output;
    }

    static synchronized void clear(Context context) {
        try {
            File file = file(context);
            if (file.exists()) {
                try (FileWriter writer = new FileWriter(file, false)) {
                    writer.write("");
                }
            }
        } catch (Exception ignored) {
        }
    }

    static String statusLabel(JSONObject event) {
        if (event == null) return "记录";
        String type = event.optString("type", "");
        JSONObject details = event.optJSONObject("details");
        if (type.contains("unauthorized") || type.contains("failed") || type.contains("error")) return "失败";
        if (details != null) {
            if (details.has("ok")) return details.optBoolean("ok", false) ? "成功" : "失败";
        }
        return "记录";
    }

    static String summary(JSONObject event) {
        if (event == null) return "";
        String type = event.optString("type", "event");
        JSONObject details = event.optJSONObject("details");
        if (details == null) details = new JSONObject();
        switch (type) {
            case "app_open":
                return "打开主界面";
            case "service_start_requested":
                return "请求启动远端控制";
            case "service_stop_requested":
                return "请求停止远端控制";
            case "accessibility_control_requested":
                return "打开手机控制权限";
            case "accessibility_settings_opened":
                return "系统权限页已打开";
            case "accessibility_settings_open_failed":
                return "系统权限页打开失败：" + brief(details.optString("error", ""));
            case "agent_node_started":
                return "本机服务已启动";
            case "agent_node_on_start_command":
                return "本机服务保持运行";
            case "agent_node_stopped":
                return "本机服务已停止";
            case "agent_node_start_failed":
                return "本机服务启动失败：" + brief(details.optString("error", ""));
            case "http_unauthorized":
                return "MCP/本机接口认证失败：" + details.optString("path", "");
            case "mcp_request":
                return "MCP 请求：" + firstNonEmpty(details.optString("tool_name", ""), details.optString("method", ""));
            case "tool_call_request":
                return "工具桥请求：" + details.optString("tool", "");
            case "sdk_observe":
                return "观察手机：" + firstNonEmpty(details.optString("active_package", ""), "未识别前台应用");
            case "capture_screenshot":
                return "截屏 " + details.optInt("width") + " x " + details.optInt("height");
            case "tap_screen":
                return "点击屏幕 (" + Math.round(details.optDouble("x")) + ", " + Math.round(details.optDouble("y")) + ")";
            case "swipe_screen":
                return "滑动屏幕 " + Math.round(details.optDouble("start_x")) + "," + Math.round(details.optDouble("start_y"))
                        + " -> " + Math.round(details.optDouble("end_x")) + "," + Math.round(details.optDouble("end_y"));
            case "input_text":
                return "输入文本，长度 " + details.optInt("length");
            case "press_back":
                return "返回";
            case "press_home":
                return "回到桌面";
            case "press_recents":
                return "打开最近任务";
            case "open_app":
                return "打开 App：" + details.optString("package_name", "");
            case "open_url":
                return "打开链接：" + urlSummary(details.optJSONObject("url"));
            case "pause_agent":
                return "透传模式继续执行";
            case "pause_agent_passthrough":
                return "透传模式继续执行";
            case "resume_agent":
                return "恢复远端控制";
            case "stop_agent":
                return "停止远端控制";
            case "cloud_command_completed":
                return "云端命令完成：" + details.optString("tool_name", "");
            case "cloud_command_failed":
                return "云端命令失败：" + details.optString("tool_name", "");
            case "phonebridge_command_completed":
                return "PhoneBridge 命令完成：" + details.optString("tool_name", "");
            case "phonebridge_command_failed":
                return "PhoneBridge 命令失败：" + details.optString("tool_name", "");
            case "phonebridge_connected":
                return "PhoneBridge 已连接";
            case "phonebridge_connection_error":
                return "PhoneBridge 连接异常：" + brief(details.optString("error", ""));
            default:
                if (type.endsWith("_launch_sent")) return "拉起目标：" + details.optString("context", "");
                if (type.endsWith("_error")) return "执行异常：" + brief(details.optString("error", ""));
                return type + " " + brief(details.toString());
        }
    }

    private static File file(Context context) {
        return new File(context.getFilesDir(), FILE_NAME);
    }

    private static List<JSONObject> readEvents(File file) {
        List<JSONObject> events = new ArrayList<>();
        if (file == null || !file.exists()) return events;
        try (BufferedReader reader = new BufferedReader(new FileReader(file))) {
            String line;
            while ((line = reader.readLine()) != null) {
                if (line.trim().isEmpty()) continue;
                try {
                    events.add(new JSONObject(line));
                } catch (Exception ignored) {
                }
            }
        } catch (Exception ignored) {
        }
        return events;
    }

    private static void writeTail(File file, List<JSONObject> events, int limit) throws Exception {
        int start = Math.max(0, events.size() - limit);
        try (FileWriter writer = new FileWriter(file, false)) {
            for (int i = start; i < events.size(); i++) {
                writer.write(events.get(i).toString());
                writer.write('\n');
            }
        }
    }

    private static Object sanitizeValue(Object value) throws Exception {
        if (value instanceof JSONObject) return sanitizeJson((JSONObject) value);
        if (value instanceof JSONArray) return sanitizeArray((JSONArray) value);
        if (value instanceof String) return sanitizeText((String) value);
        return value;
    }

    private static JSONObject sanitizeJson(JSONObject input) throws Exception {
        JSONObject output = new JSONObject();
        Iterator<String> iterator = input.keys();
        while (iterator.hasNext()) {
            String key = iterator.next();
            String normalized = key.toLowerCase(Locale.ROOT);
            if (normalized.contains("token")
                    || normalized.contains("authorization")
                    || normalized.contains("password")
                    || normalized.contains("secret")
                    || normalized.contains("signature")
                    || normalized.contains("otp")) {
                output.put(key, "[redacted]");
            } else {
                output.put(key, sanitizeValue(input.get(key)));
            }
        }
        return output;
    }

    private static JSONArray sanitizeArray(JSONArray input) throws Exception {
        JSONArray output = new JSONArray();
        for (int i = 0; i < input.length(); i++) {
            output.put(sanitizeValue(input.get(i)));
        }
        return output;
    }

    private static String sanitizeText(String value) {
        String output = value == null ? "" : value;
        output = output.replaceAll("(?i)Bearer\\s+[A-Za-z0-9._~+\\-/=]+", "Bearer [redacted]");
        output = output.replaceAll("(?i)sk-[A-Za-z0-9_\\-]{12,}", "[redacted-key]");
        output = output.replaceAll("(?i)(token|access_token|refresh_token|authorization|password|secret|otp|验证码)\\s*[:=]\\s*[^\\s,;\\}\"']+", "$1=[redacted]");
        return brief(output);
    }

    private static String urlSummary(JSONObject url) {
        if (url == null) return "";
        String host = url.optString("host", "");
        String path = url.optString("path", "");
        return host + (path.isEmpty() ? "" : path);
    }

    private static String firstNonEmpty(String... values) {
        if (values == null) return "";
        for (String value : values) {
            if (value != null && !value.trim().isEmpty()) return value.trim();
        }
        return "";
    }

    private static String brief(String value) {
        if (value == null) return "";
        String compact = value.replace('\n', ' ').replace('\r', ' ').trim();
        if (compact.length() <= MAX_TEXT_LENGTH) return compact;
        return compact.substring(0, MAX_TEXT_LENGTH) + "...";
    }
}
