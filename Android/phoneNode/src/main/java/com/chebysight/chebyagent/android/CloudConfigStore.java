package com.chebysight.chebyagent.android;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Base64;

import java.net.URI;
import java.security.SecureRandom;
import java.util.Locale;

final class CloudConfigStore {
    private static final String PREFS = "chebyagent_cloud";
    private static final String KEY_BASE_URL = "base_url";
    private static final String KEY_DEVICE_ID = "device_id";
    private static final String DEFAULT_BASE_URL = BuildConfig.PHONEBRIDGE_DEFAULT_BASE_URL;

    private CloudConfigStore() {
    }

    static String baseUrl(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String value = prefs.getString(KEY_BASE_URL, DEFAULT_BASE_URL);
        if (value == null || value.trim().isEmpty()) return DEFAULT_BASE_URL;
        return trimTrailingSlash(value.trim());
    }

    static String phoneBridgeUrl(Context context) {
        String base = baseUrl(context);
        String lower = base.toLowerCase(Locale.US);
        if (lower.startsWith("ws://") || lower.startsWith("wss://")) {
            return ensureConnectPath(base);
        }
        if (lower.startsWith("https://")) {
            return ensureConnectPath("wss://" + base.substring("https://".length()));
        }
        if (lower.startsWith("http://")) {
            return ensureConnectPath("ws://" + base.substring("http://".length()));
        }
        return ensureConnectPath("wss://" + base);
    }

    static URI phoneBridgeUri(Context context) throws Exception {
        return new URI(phoneBridgeUrl(context));
    }

    static synchronized String getOrCreateDeviceId(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String existing = prefs.getString(KEY_DEVICE_ID, null);
        if (existing != null && existing.length() >= 16) return existing;

        byte[] random = new byte[18];
        new SecureRandom().nextBytes(random);
        String id = "android-" + Base64.encodeToString(random, Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING);
        prefs.edit().putString(KEY_DEVICE_ID, id).apply();
        return id;
    }

    private static String trimTrailingSlash(String value) {
        while (value.endsWith("/") && value.length() > 1) value = value.substring(0, value.length() - 1);
        return value;
    }

    private static String ensureConnectPath(String value) {
        String trimmed = trimTrailingSlash(value);
        if (trimmed.endsWith("/phone/connect")) return trimmed;
        return trimmed + "/phone/connect";
    }
}
