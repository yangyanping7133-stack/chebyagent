package com.cheby.codex.updater;

import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;
import android.content.pm.PackageManager;
import android.os.Build;

import java.io.FileInputStream;
import java.io.IOException;
import java.io.OutputStream;

final class UpdateInstaller {
    private UpdateInstaller() {}

    static int install(Context context, ApkVerifier.VerifiedApk apk, String packageName)
            throws IOException {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
            throw new IOException("silent device-owner install requires Android 12 or newer");
        }
        PackageInstaller packageInstaller = context.getPackageManager().getPackageInstaller();
        PackageInstaller.SessionParams params = new PackageInstaller.SessionParams(
                PackageInstaller.SessionParams.MODE_FULL_INSTALL);
        params.setAppPackageName(packageName);
        params.setInstallReason(PackageManager.INSTALL_REASON_POLICY);
        params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED);

        int sessionId = packageInstaller.createSession(params);
        try (PackageInstaller.Session session = packageInstaller.openSession(sessionId);
             FileInputStream input = new FileInputStream(apk.file());
             OutputStream output = session.openWrite("base.apk", 0, apk.file().length())) {
            byte[] buffer = new byte[64 * 1024];
            int count;
            while ((count = input.read(buffer)) > 0) {
                output.write(buffer, 0, count);
            }
            session.fsync(output);

            Intent resultIntent = new Intent(context, UpdateResultReceiver.class)
                    .setAction(UpdateResultReceiver.ACTION_INSTALL_RESULT)
                    .putExtra(UpdateResultReceiver.EXTRA_SESSION_ID, sessionId);
            PendingIntent result = PendingIntent.getBroadcast(
                    context,
                    sessionId,
                    resultIntent,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_MUTABLE);
            session.commit(result.getIntentSender());
            return sessionId;
        } catch (IOException | RuntimeException error) {
            packageInstaller.abandonSession(sessionId);
            throw error;
        }
    }
}
