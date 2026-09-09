package com.cheby.codex.updater;

import android.content.Context;
import android.content.SharedPreferences;

final class UpdateStatusStore {
    private static final String PREFERENCES = "update_status";

    private UpdateStatusStore() {}

    static void record(Context context, String state, int sessionId, int status) {
        context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)
                .edit()
                .putString("state", state)
                .putInt("session_id", sessionId)
                .putInt("status", status)
                .putLong("updated_at_ms", System.currentTimeMillis())
                .apply();
    }

    static Snapshot read(Context context) {
        SharedPreferences preferences = context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE);
        return new Snapshot(
                preferences.getString("state", "never_run"),
                preferences.getInt("session_id", -1),
                preferences.getInt("status", 0),
                preferences.getLong("updated_at_ms", 0));
    }

    record Snapshot(String state, int sessionId, int status, long updatedAtMs) {}
}
