package com.cheby.codex.updater;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;

public final class UpdateResultReceiver extends BroadcastReceiver {
    static final String ACTION_INSTALL_RESULT = "com.cheby.codex.updater.action.INSTALL_RESULT";
    static final String EXTRA_SESSION_ID = "session_id";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (!ACTION_INSTALL_RESULT.equals(intent.getAction())) {
            return;
        }
        int sessionId = intent.getIntExtra(EXTRA_SESSION_ID, -1);
        int status = intent.getIntExtra(
                PackageInstaller.EXTRA_STATUS,
                PackageInstaller.STATUS_FAILURE);
        String state = status == PackageInstaller.STATUS_SUCCESS ? "installed" : "failed";
        UpdateStatusStore.record(context, state, sessionId, status);
    }
}
