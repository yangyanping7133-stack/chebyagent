package com.chebysight.chebyagent.android;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.DisplayMetrics;
import android.view.WindowManager;

import java.nio.ByteBuffer;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

/** Android 10 MediaProjection fallback; Android 11+ continues to use Accessibility screenshots. */
final class MediaProjectionScreenshotCapture {
    private static final Object LOCK = new Object();
    private static MediaProjection projection;
    private static boolean permissionRequestActive;
    private static long permissionRequestStartedAtMillis;
    private static final long PERMISSION_REQUEST_STALE_MS = 15_000L;

    private MediaProjectionScreenshotCapture() {}

    static Bitmap captureOrRequest(Context context) throws Exception {
        MediaProjection current;
        boolean launchPermission = false;
        synchronized (LOCK) {
            current = projection;
            if (current == null) {
                long now = System.currentTimeMillis();
                if (!permissionRequestActive || now - permissionRequestStartedAtMillis >= PERMISSION_REQUEST_STALE_MS) {
                    permissionRequestActive = true;
                    permissionRequestStartedAtMillis = now;
                    launchPermission = true;
                }
            }
        }
        if (current == null && launchPermission) {
            Intent permission = new Intent(
                    context.getApplicationContext(),
                    MediaProjectionPermissionActivity.class
            ).addFlags(
                    Intent.FLAG_ACTIVITY_NEW_TASK |
                            Intent.FLAG_ACTIVITY_EXCLUDE_FROM_RECENTS |
                            Intent.FLAG_ACTIVITY_SINGLE_TOP
            );
            try {
                launchPermissionActivity(context, permission);
                AgentAccessibilityService service = AgentAccessibilityService.getInstance();
                if (service == null || !service.approveOwnMediaProjectionConsent(6_000L)) {
                    throw new IllegalStateException(
                            "Android 10 screenshot consent did not reach the exact ChebyNode system dialog."
                    );
                }
            } catch (Exception error) {
                permissionFailed();
                throw new IllegalStateException(
                        "Android 10 screenshot permission could not be established.",
                        error
                );
            }
        }
        if (current == null) current = awaitProjection(8_000L);
        if (current == null) {
            permissionFailed();
            throw new IllegalStateException(
                    "Android 10 screenshot permission was not granted in time."
            );
        }
        return capture(context, current);
    }

    static void acceptPermission(Context context, int resultCode, Intent data) {
        if (resultCode != Activity.RESULT_OK || data == null) {
            permissionFailed();
            return;
        }
        MediaProjectionManager manager = context.getSystemService(MediaProjectionManager.class);
        if (manager == null) {
            permissionFailed();
            throw new IllegalStateException("MediaProjection service is unavailable.");
        }
        MediaProjection next = manager.getMediaProjection(resultCode, data);
        if (next == null) {
            permissionFailed();
            throw new IllegalStateException("MediaProjection permission was not granted.");
        }
        next.registerCallback(new MediaProjection.Callback() {
            @Override
            public void onStop() {
                synchronized (LOCK) {
                    if (projection == next) projection = null;
                }
            }
        }, new Handler(context.getMainLooper()));
        synchronized (LOCK) {
            MediaProjection prior = projection;
            projection = next;
            permissionRequestActive = false;
            permissionRequestStartedAtMillis = 0L;
            if (prior != null) prior.stop();
        }
    }

    static void permissionFailed() {
        synchronized (LOCK) {
            permissionRequestActive = false;
            permissionRequestStartedAtMillis = 0L;
        }
    }

    static void stop() {
        synchronized (LOCK) {
            permissionRequestActive = false;
            permissionRequestStartedAtMillis = 0L;
            MediaProjection prior = projection;
            projection = null;
            if (prior != null) prior.stop();
        }
    }

    private static void launchPermissionActivity(Context context, Intent intent) throws Exception {
        AgentAccessibilityService service = AgentAccessibilityService.getInstance();
        if (service == null) {
            throw new IllegalStateException("Accessibility service is unavailable for screenshot consent.");
        }
        if (context.getPackageName().equals(service.currentForegroundPackage())) {
            try {
                context.startActivity(intent);
                AuditLog.append(context, "media_projection_permission_launch", JsonUtil.obj(
                        "context", "foreground_application"
                ));
                return;
            } catch (Exception ignored) {
                // Fall through to the verified launcher trampoline.
            }
        }
        PendingTargetLaunch.Request pending = PendingTargetLaunch.begin(
                intent,
                "media_projection_permission"
        );
        try {
            if (!service.openOwnLauncherActivity(3_500L)) {
                throw new IllegalStateException("ChebyNode launcher icon could not be activated.");
            }
            if (!pending.await(4_500L)) {
                String suffix = pending.error().isEmpty() ? "" : " " + pending.error();
                throw new IllegalStateException(
                        "ChebyNode foreground trampoline did not dispatch screenshot consent." + suffix
                );
            }
            AuditLog.append(context, "media_projection_permission_launch", JsonUtil.obj(
                    "context", "foreground_trampoline",
                    "request_id", pending.requestId()
            ));
        } finally {
            PendingTargetLaunch.cancel(pending, "Screenshot consent launch scope ended.");
        }
    }

    private static MediaProjection awaitProjection(long timeoutMs) throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            synchronized (LOCK) {
                if (projection != null) return projection;
            }
            Thread.sleep(100L);
        }
        synchronized (LOCK) {
            return projection;
        }
    }

    private static Bitmap capture(Context context, MediaProjection current) throws Exception {
        WindowManager windowManager = (WindowManager) context.getSystemService(Context.WINDOW_SERVICE);
        DisplayMetrics metrics = new DisplayMetrics();
        if (windowManager == null || windowManager.getDefaultDisplay() == null) {
            throw new IllegalStateException("Display service is unavailable.");
        }
        windowManager.getDefaultDisplay().getRealMetrics(metrics);
        int width = metrics.widthPixels;
        int height = metrics.heightPixels;
        int density = metrics.densityDpi;
        ImageReader reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2);
        HandlerThread thread = new HandlerThread("chebynode-screenshot");
        thread.start();
        Handler handler = new Handler(thread.getLooper());
        CountDownLatch latch = new CountDownLatch(1);
        AtomicBoolean claimed = new AtomicBoolean(false);
        AtomicReference<Bitmap> bitmap = new AtomicReference<>();
        AtomicReference<Throwable> failure = new AtomicReference<>();
        reader.setOnImageAvailableListener(source -> {
            if (!claimed.compareAndSet(false, true)) return;
            Image image = null;
            Bitmap padded = null;
            try {
                image = source.acquireLatestImage();
                if (image == null) throw new IllegalStateException("Screenshot returned no image.");
                Image.Plane plane = image.getPlanes()[0];
                ByteBuffer buffer = plane.getBuffer();
                int pixelStride = plane.getPixelStride();
                int rowStride = plane.getRowStride();
                int paddedWidth = width + Math.max(0, rowStride - pixelStride * width) / pixelStride;
                padded = Bitmap.createBitmap(paddedWidth, height, Bitmap.Config.ARGB_8888);
                padded.copyPixelsFromBuffer(buffer);
                Bitmap cropped = Bitmap.createBitmap(padded, 0, 0, width, height);
                bitmap.set(cropped);
                if (cropped == padded) padded = null;
            } catch (Throwable error) {
                failure.set(error);
            } finally {
                if (padded != null) padded.recycle();
                if (image != null) image.close();
                latch.countDown();
            }
        }, handler);
        VirtualDisplay display = null;
        try {
            display = current.createVirtualDisplay(
                    "ChebyNodeScreenshot",
                    width,
                    height,
                    density,
                    DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                    reader.getSurface(),
                    null,
                    handler
            );
            if (display == null) throw new IllegalStateException("Screenshot display could not start.");
            if (!latch.await(10, TimeUnit.SECONDS)) {
                throw new IllegalStateException("Timed out while taking MediaProjection screenshot.");
            }
            if (failure.get() != null) {
                throw new IllegalStateException("MediaProjection screenshot failed.", failure.get());
            }
            Bitmap result = bitmap.get();
            if (result == null) throw new IllegalStateException("MediaProjection returned no bitmap.");
            return result;
        } finally {
            reader.setOnImageAvailableListener(null, null);
            if (display != null) display.release();
            reader.close();
            thread.quitSafely();
        }
    }
}
