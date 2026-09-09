package com.chebysight.chebyagent.android;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;

import org.json.JSONObject;

public class AgentNodeService extends Service {
    static final String ACTION_START_MEDIA_PROJECTION =
            "com.chebysight.chebyagent.phonenode.START_MEDIA_PROJECTION";
    static final String EXTRA_MEDIA_PROJECTION_RESULT_CODE = "media_projection_result_code";
    static final String EXTRA_MEDIA_PROJECTION_DATA = "media_projection_data";
    private static final String CHANNEL_ID = "chebyagent_agent_node";
    private static final int NOTIFICATION_ID = 1001;
    private static volatile boolean running = false;

    private PhoneBridgeClient phoneBridgeClient;
    private ManagedAccessibilityRecovery accessibilityRecovery;

    static boolean isRunning() {
        return running;
    }

    static boolean isPaused() {
        return false;
    }

    static void setPaused(boolean value) {
        AgentAccessibilityService accessibilityService = AgentAccessibilityService.getInstance();
        if (accessibilityService != null && running) {
            accessibilityService.hideAgentOverlay();
        }
    }

    static void ensureStarted(Context context, String reason) {
        Context appContext = context.getApplicationContext();
        try {
            Intent intent = new Intent(appContext, AgentNodeService.class);
            intent.putExtra("start_reason", reason == null ? "" : reason);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                appContext.startForegroundService(intent);
            } else {
                appContext.startService(intent);
            }
            AuditLog.append(appContext, "agent_node_start_requested", JsonUtil.obj("reason", reason == null ? "" : reason));
        } catch (Exception error) {
            AuditLog.append(appContext, "agent_node_start_request_failed", JsonUtil.obj(
                    "reason", reason == null ? "" : reason,
                    "message", error.getMessage() == null ? String.valueOf(error) : error.getMessage()
            ));
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        accessibilityRecovery = createAccessibilityRecovery();
        accessibilityRecovery.request();
        createNotificationChannel();
        startForeground(NOTIFICATION_ID, notification());
        startPhoneBridge();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        accessibilityRecovery.request();
        AuditLog.append(this, "agent_node_on_start_command", JsonUtil.obj(
                "reason", intent == null ? "" : intent.getStringExtra("start_reason"),
                "flags", flags,
                "start_id", startId
        ));
        if (intent != null && ACTION_START_MEDIA_PROJECTION.equals(intent.getAction())) {
            int resultCode = intent.getIntExtra(
                    EXTRA_MEDIA_PROJECTION_RESULT_CODE,
                    android.app.Activity.RESULT_CANCELED
            );
            Intent resultData = intent.getParcelableExtra(EXTRA_MEDIA_PROJECTION_DATA);
            try {
                enableMediaProjectionForeground();
                MediaProjectionScreenshotCapture.acceptPermission(this, resultCode, resultData);
            } catch (Exception error) {
                MediaProjectionScreenshotCapture.permissionFailed();
                AuditLog.append(this, "media_projection_start_failed", JsonUtil.obj(
                        "message", error.getMessage() == null ? String.valueOf(error) : error.getMessage()
                ));
            }
        }
        startPhoneBridge();
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        if (accessibilityRecovery != null) accessibilityRecovery.close();
        AuditLog.append(this, "agent_node_stopped", new JSONObject());
        running = false;
        if (phoneBridgeClient != null) phoneBridgeClient.stop();
        phoneBridgeClient = null;
        MediaProjectionScreenshotCapture.stop();
        AgentAccessibilityService accessibilityService = AgentAccessibilityService.getInstance();
        if (accessibilityService != null) {
            accessibilityService.hideAgentOverlay();
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private synchronized void startPhoneBridge() {
        if (phoneBridgeClient != null) return;
        try {
            if (!BuildConfig.PHONEBRIDGE_ENABLED) {
                throw new IllegalStateException("PhoneBridge is disabled");
            }
            phoneBridgeClient = new PhoneBridgeClient(this);
            phoneBridgeClient.start();
            running = true;
            AgentAccessibilityService accessibilityService = AgentAccessibilityService.getInstance();
            if (accessibilityService != null) {
                accessibilityService.hideAgentOverlay();
            }
            AuditLog.append(this, "phone_device_executor_started", new JSONObject());
        } catch (Exception error) {
            if (phoneBridgeClient != null) phoneBridgeClient.stop();
            phoneBridgeClient = null;
            running = false;
            AuditLog.append(this, "phone_device_executor_start_failed", JsonUtil.obj("error", error.getMessage()));
        }
    }

    private ManagedAccessibilityRecovery createAccessibilityRecovery() {
        Handler handler = new Handler(Looper.getMainLooper());
        return new ManagedAccessibilityRecovery(new ManagedAccessibilityRecovery.Driver() {
            @Override public boolean permitted() {
                return checkSelfPermission(Manifest.permission.WRITE_SECURE_SETTINGS)
                        == PackageManager.PERMISSION_GRANTED;
            }
            @Override public boolean enabled() {
                return AccessibilityState.isServiceEnabled(AgentNodeService.this);
            }
            @Override public boolean bound() {
                return AgentAccessibilityService.getInstance() != null;
            }
            @Override public boolean enable() {
                return AccessibilityState.enableForManagedDevice(AgentNodeService.this);
            }
            @Override public boolean removeOwnService() {
                return AccessibilityState.removeOwnServiceForManagedRebind(AgentNodeService.this);
            }
            @Override public void report(String phase, boolean enabled, boolean bound) {
                AuditLog.append(AgentNodeService.this, "managed_accessibility_checked",
                        JsonUtil.obj("phase", phase, "enabled", enabled, "bound", bound,
                                "ok", enabled && bound));
            }
        }, new ManagedAccessibilityRecovery.Scheduler() {
            @Override public void post(Runnable task, long delayMillis) {
                handler.postDelayed(task, delayMillis);
            }
            @Override public void cancel(Runnable task) { handler.removeCallbacks(task); }
        });
    }

    private Notification notification() {
        Intent intent = getPackageManager().getLaunchIntentForPackage(getPackageName());
        if (intent == null) intent = new Intent(this, MainActivity.class);
        PendingIntent pendingIntent = PendingIntent.getActivity(
                this,
                0,
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );

        Notification.Builder builder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);

        return builder
                .setContentTitle(getString(com.chebysight.chebyagent.android.R.string.agent_node_notification_title))
                .setContentText(getString(com.chebysight.chebyagent.android.R.string.agent_node_notification_body))
                .setSmallIcon(android.R.drawable.stat_sys_upload_done)
                .setContentIntent(pendingIntent)
                .setOngoing(true)
                .build();
    }

    private void enableMediaProjectionForeground() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                    NOTIFICATION_ID,
                    notification(),
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC |
                            ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
            );
        } else {
            startForeground(NOTIFICATION_ID, notification());
        }
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID,
                BuildConfig.AGENT_NODE_PRODUCT_NAME + " AgentNode",
                NotificationManager.IMPORTANCE_LOW
        );
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (manager != null) manager.createNotificationChannel(channel);
    }
}
