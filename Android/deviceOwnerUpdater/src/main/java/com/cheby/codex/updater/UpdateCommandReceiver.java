package com.cheby.codex.updater;

import android.app.admin.DevicePolicyManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class UpdateCommandReceiver extends BroadcastReceiver {
    public static final String ACTION_PREPARE = "com.cheby.codex.updater.action.PREPARE";
    public static final String ACTION_INSTALL = "com.cheby.codex.updater.action.INSTALL";
    public static final String EXTRA_FILE = "file";
    public static final String EXTRA_PACKAGE = "package";
    public static final String EXTRA_SHA256 = "sha256";

    private static final ExecutorService EXECUTOR = Executors.newSingleThreadExecutor();

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        if (!ACTION_PREPARE.equals(action) && !ACTION_INSTALL.equals(action)) {
            return;
        }
        PendingResult pendingResult = goAsync();
        Context appContext = context.getApplicationContext();
        String fileName = intent.getStringExtra(EXTRA_FILE);
        String packageName = intent.getStringExtra(EXTRA_PACKAGE);
        String sha256 = intent.getStringExtra(EXTRA_SHA256);
        EXECUTOR.execute(() -> {
            try {
                DevicePolicyManager policyManager = appContext.getSystemService(
                        DevicePolicyManager.class);
                if (policyManager == null || !policyManager.isDeviceOwnerApp(appContext.getPackageName())) {
                    UpdateStatusStore.record(appContext, "rejected_not_device_owner", -1, -1);
                    return;
                }
                if (ACTION_PREPARE.equals(action)) {
                    UpdatePaths.ensureUpdatesDirectory(appContext);
                    UpdateStatusStore.record(appContext, "ready", -1, 0);
                    return;
                }
                InstallPolicy.Command command = InstallPolicy.validateCommand(
                        BuildConfig.TARGET_PACKAGE,
                        packageName,
                        fileName,
                        sha256);
                ApkVerifier.VerifiedApk apk = ApkVerifier.verify(
                        appContext,
                        UpdatePaths.ensureUpdatesDirectory(appContext),
                        command);
                int sessionId = UpdateInstaller.install(appContext, apk, command.packageName());
                UpdateStatusStore.record(appContext, "submitted", sessionId, 0);
            } catch (Exception error) {
                UpdateStatusStore.record(appContext, "rejected", -1, -1);
            } finally {
                pendingResult.finish();
            }
        });
    }
}
