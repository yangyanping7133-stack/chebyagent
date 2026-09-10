package com.cheby.codex.runtime;

import android.app.Activity;
import android.content.Context;
import android.net.Uri;

/** Narrow product boundary between Cheby UI and an APK-owned execution environment. */
public interface EmbeddedRuntime {
    /** Returns editable configuration metadata, never saved credentials. */
    default String providerSettings() { return "{}"; }

    /** Null means persisted; connection and model capability still require verification. */
    default String saveProviderSettings(String json) { return "当前版本没有内置运行环境。"; }

    /** Explicitly confirmed recovery of unreadable credentials; callers must hold an idle-turn guard. */
    default String rebuildProviderSettings() { return "当前版本没有内置运行环境。"; }

    /** Safe user-facing failure detail. Never returns raw logs, paths, or exception messages. */
    default String failureDetail() { return ""; }

    /** Null means a data-preserving retry was started. */
    default String retryProvisioning() { return "当前版本没有内置运行环境。"; }

    void prepare(Activity activity);

    EmbeddedRuntimeStartResult ensureStarted(String tokenSha256);

    default EmbeddedRuntimeStatus status() {
        return EmbeddedRuntimeStatus.UNAVAILABLE;
    }

    default EmbeddedRuntimeImportResult importCodexAuth(Context context, Uri uri) {
        return EmbeddedRuntimeImportResult.UNAVAILABLE;
    }
}
