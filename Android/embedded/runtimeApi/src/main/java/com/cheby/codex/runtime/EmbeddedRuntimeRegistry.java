package com.cheby.codex.runtime;

import android.app.Activity;
import android.content.Context;
import android.net.Uri;

import java.util.Objects;
import java.util.concurrent.atomic.AtomicReference;

/** Process-local registry installed by the appliance Application before any Activity is created. */
public final class EmbeddedRuntimeRegistry {
    private static final AtomicReference<EmbeddedRuntime> OWNER = new AtomicReference<>();

    private EmbeddedRuntimeRegistry() {}

    public static String providerSettings() {
        EmbeddedRuntime runtime = OWNER.get();
        return runtime == null ? "{}" : runtime.providerSettings();
    }

    public static String saveProviderSettings(String json) {
        EmbeddedRuntime runtime = OWNER.get();
        return runtime == null ? "当前版本没有内置运行环境。" : runtime.saveProviderSettings(json);
    }

    public static String rebuildProviderSettings() {
        EmbeddedRuntime runtime = OWNER.get();
        return runtime == null ? "当前版本没有内置运行环境。" : runtime.rebuildProviderSettings();
    }

    public static String failureDetail() {
        EmbeddedRuntime runtime = OWNER.get();
        return runtime == null ? "" : runtime.failureDetail();
    }

    public static String retryProvisioning() {
        EmbeddedRuntime runtime = OWNER.get();
        return runtime == null ? "当前版本没有内置运行环境。" : runtime.retryProvisioning();
    }

    public static void install(EmbeddedRuntime runtime) {
        Objects.requireNonNull(runtime, "runtime");
        if (!OWNER.compareAndSet(null, runtime)) {
            throw new IllegalStateException("Embedded runtime owner is already installed");
        }
    }

    public static boolean prepareIfPresent(Activity activity) {
        EmbeddedRuntime runtime = OWNER.get();
        if (runtime == null) return false;
        runtime.prepare(Objects.requireNonNull(activity, "activity"));
        return true;
    }

    public static EmbeddedRuntimeStartResult ensureStarted(String tokenSha256) {
        EmbeddedRuntime runtime = OWNER.get();
        return runtime == null
            ? EmbeddedRuntimeStartResult.UNAVAILABLE
            : runtime.ensureStarted(tokenSha256);
    }

    public static EmbeddedRuntimeStatus status() {
        EmbeddedRuntime runtime = OWNER.get();
        return runtime == null
            ? EmbeddedRuntimeStatus.UNAVAILABLE
            : runtime.status();
    }

    public static EmbeddedRuntimeImportResult importCodexAuth(Context context, Uri uri) {
        EmbeddedRuntime runtime = OWNER.get();
        return runtime == null
            ? EmbeddedRuntimeImportResult.UNAVAILABLE
            : runtime.importCodexAuth(
                Objects.requireNonNull(context, "context"),
                Objects.requireNonNull(uri, "uri")
            );
    }
}
