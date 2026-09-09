package com.chebysight.chebyagent.android;

import android.app.Activity;
import android.app.Application;
import android.os.Bundle;

import java.util.concurrent.atomic.AtomicBoolean;

/** Connects an embedded host Activity lifecycle to one-shot PhoneNode foreground launches. */
public final class AgentNodeLifecycleBridge {
    private static final AtomicBoolean INSTALLED = new AtomicBoolean();

    private AgentNodeLifecycleBridge() {}

    public static void install(Application application) {
        if (application == null || !INSTALLED.compareAndSet(false, true)) return;
        application.registerActivityLifecycleCallbacks(new Application.ActivityLifecycleCallbacks() {
            @Override
            public void onActivityCreated(Activity activity, Bundle savedInstanceState) {}

            @Override
            public void onActivityStarted(Activity activity) {}

            @Override
            public void onActivityResumed(Activity activity) {
                PendingTargetLaunch.dispatchIfPending(activity);
            }

            @Override
            public void onActivityPaused(Activity activity) {}

            @Override
            public void onActivityStopped(Activity activity) {}

            @Override
            public void onActivitySaveInstanceState(Activity activity, Bundle outState) {}

            @Override
            public void onActivityDestroyed(Activity activity) {}
        });
    }
}
