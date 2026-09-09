package com.chebysight.chebyagent.android;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

public final class BootStartupReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        Context appContext = context.getApplicationContext();
        String action = intent == null ? "" : String.valueOf(intent.getAction());
        AuditLog.append(appContext, "boot_startup_received", JsonUtil.obj("action", action));
        AgentNodeService.ensureStarted(appContext, "boot:" + action);
    }
}
