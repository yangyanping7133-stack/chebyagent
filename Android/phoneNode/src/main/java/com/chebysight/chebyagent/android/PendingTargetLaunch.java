package com.chebysight.chebyagent.android;

import android.app.Activity;
import android.content.Intent;

import java.security.SecureRandom;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/** One-shot foreground handoff from PhoneBridge to the launcher Activity. */
final class PendingTargetLaunch {
    private static final AtomicReference<Request> PENDING = new AtomicReference<>();
    private static final SecureRandom RANDOM = new SecureRandom();

    private PendingTargetLaunch() {
    }

    static Request begin(Intent targetIntent, String action) {
        Request request = new Request(targetIntent, action, randomId());
        if (!PENDING.compareAndSet(null, request)) {
            throw new IllegalStateException("Another target launch is already in progress.");
        }
        return request;
    }

    static boolean dispatchIfPending(Activity activity) {
        Request request = PENDING.getAndSet(null);
        if (request == null) return false;
        try {
            activity.startActivity(new Intent(request.targetIntent));
            request.complete(true, "");
            AuditLog.append(activity, "target_launch_dispatched", JsonUtil.obj(
                    "action", request.action,
                    "request_id", request.requestId
            ));
        } catch (Exception error) {
            request.complete(false, safeMessage(error));
            AuditLog.append(activity, "target_launch_dispatch_failed", JsonUtil.obj(
                    "action", request.action,
                    "request_id", request.requestId,
                    "error", safeMessage(error)
            ));
        }
        return true;
    }

    static void cancel(Request request, String reason) {
        if (request == null) return;
        PENDING.compareAndSet(request, null);
        request.complete(false, reason == null ? "Target launch was cancelled." : reason);
    }

    static void cancelAll(String reason) {
        Request request = PENDING.getAndSet(null);
        if (request != null) request.complete(false, reason);
    }

    private static String randomId() {
        byte[] bytes = new byte[12];
        RANDOM.nextBytes(bytes);
        StringBuilder output = new StringBuilder(bytes.length * 2);
        for (byte value : bytes) output.append(String.format("%02x", value & 0xff));
        return output.toString();
    }

    private static String safeMessage(Exception error) {
        String message = error.getMessage();
        return message == null || message.trim().isEmpty() ? String.valueOf(error) : message;
    }

    static final class Request {
        private final Intent targetIntent;
        private final String action;
        private final String requestId;
        private final CountDownLatch completion = new CountDownLatch(1);
        private volatile boolean dispatched;
        private volatile String error = "";

        private Request(Intent targetIntent, String action, String requestId) {
            this.targetIntent = new Intent(targetIntent);
            this.action = action == null ? "" : action;
            this.requestId = requestId;
        }

        boolean await(long timeoutMs) throws InterruptedException {
            return completion.await(timeoutMs, TimeUnit.MILLISECONDS) && dispatched;
        }

        String error() {
            return error;
        }

        String requestId() {
            return requestId;
        }

        private void complete(boolean wasDispatched, String failure) {
            dispatched = wasDispatched;
            error = failure == null ? "" : failure;
            completion.countDown();
        }
    }
}
