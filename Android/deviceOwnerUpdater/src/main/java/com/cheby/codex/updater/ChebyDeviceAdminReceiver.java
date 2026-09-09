package com.cheby.codex.updater;

import android.app.admin.DeviceAdminReceiver;
import android.content.Context;
import android.content.Intent;

/** Device-owner marker. No broad device restrictions are requested. */
public final class ChebyDeviceAdminReceiver extends DeviceAdminReceiver {
    @Override
    public void onEnabled(Context context, Intent intent) {
        super.onEnabled(context, intent);
        try {
            UpdatePaths.ensureUpdatesDirectory(context);
            UpdateStatusStore.record(context, "ready", -1, 0);
        } catch (Exception error) {
            UpdateStatusStore.record(context, "storage_unavailable", -1, -1);
        }
    }
}
