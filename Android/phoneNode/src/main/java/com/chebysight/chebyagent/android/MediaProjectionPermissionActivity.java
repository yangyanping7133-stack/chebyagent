package com.chebysight.chebyagent.android;

import android.app.Activity;
import android.content.Intent;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Bundle;

/** System-consent trampoline used only for the Android 10 screenshot fallback. */
public final class MediaProjectionPermissionActivity extends Activity {
    private static final int REQUEST_CAPTURE = 7041;
    private boolean requestStarted;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            MediaProjectionScreenshotCapture.permissionFailed();
            finish();
            return;
        }
        requestStarted = savedInstanceState != null &&
                savedInstanceState.getBoolean("request_started", false);
        if (requestStarted) return;
        requestStarted = true;
        MediaProjectionManager manager = getSystemService(MediaProjectionManager.class);
        if (manager == null) {
            MediaProjectionScreenshotCapture.permissionFailed();
            finish();
            return;
        }
        startActivityForResult(manager.createScreenCaptureIntent(), REQUEST_CAPTURE);
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        outState.putBoolean("request_started", requestStarted);
        super.onSaveInstanceState(outState);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQUEST_CAPTURE || resultCode != RESULT_OK || data == null) {
            MediaProjectionScreenshotCapture.permissionFailed();
            finish();
            return;
        }
        Intent service = new Intent(this, AgentNodeService.class)
                .setAction(AgentNodeService.ACTION_START_MEDIA_PROJECTION)
                .putExtra(AgentNodeService.EXTRA_MEDIA_PROJECTION_RESULT_CODE, resultCode)
                .putExtra(AgentNodeService.EXTRA_MEDIA_PROJECTION_DATA, data);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(service);
        } else {
            startService(service);
        }
        finish();
    }
}
