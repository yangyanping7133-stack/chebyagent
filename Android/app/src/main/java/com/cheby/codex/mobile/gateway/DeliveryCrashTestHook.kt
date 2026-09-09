package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.BuildConfig
import com.cheby.codex.mobile.model.ThreadKey

/**
 * Exact process-death checkpoints used only by the isolated Gate APK.
 *
 * Production callers always receive [NoOpDeliveryCrashTestHook]. A non-no-op implementation is
 * rejected unless this is the debuggable `.gate` application on TCP 27462, so release and
 * migration APKs cannot be paused at a delivery boundary. The Gate implementation itself lives in
 * androidTest and is therefore absent from every shipping APK.
 */
internal enum class DeliveryCrashCheckpoint {
    ENQUEUE_PERSISTED,
    NETWORK_SENT_UNCONFIRMED,
    TRANSPORT_ACCEPTED,
    ANDROID_ACCEPTED,
    OUTBOX_CLEANED,
}

internal data class DeliveryCrashIdentity(
    val sessionScope: String,
    val sessionGeneration: Long,
    val threadKey: ThreadKey,
    val clientMessageId: String,
    val turnId: String? = null,
)

internal interface DeliveryCrashTestHook {
    val isNoOp: Boolean

    /**
     * Implementations may block forever after durably publishing evidence. The host must then
     * terminate the real Android process; returning normally is not crash evidence.
     */
    fun reached(checkpoint: DeliveryCrashCheckpoint, identity: DeliveryCrashIdentity)
}

internal object NoOpDeliveryCrashTestHook : DeliveryCrashTestHook {
    override val isNoOp: Boolean = true

    override fun reached(
        checkpoint: DeliveryCrashCheckpoint,
        identity: DeliveryCrashIdentity,
    ) = Unit
}

internal fun requireGateOnlyDeliveryCrashHook(hook: DeliveryCrashTestHook) {
    require(
        hook.isNoOp ||
            (
                BuildConfig.DEBUG &&
                    BuildConfig.APPLICATION_ID == "com.cheby.codex.mobile.gate" &&
                    BuildConfig.RELAY_SERVICE_PORT == 27462
                )
    ) {
        "Delivery crash hooks are restricted to the isolated Gate APK"
    }
}

/**
 * Keeping the build-type condition at every call site lets R8 constant-fold shipping calls and
 * devirtualize the surviving hook path to [NoOpDeliveryCrashTestHook]. Supporting identity types
 * may remain in optimized method signatures; the security boundary is that the only non-no-op
 * implementation is androidTest-only and cannot be present in a shipping APK.
 */
internal fun DeliveryCrashTestHook.reachedForGate(
    checkpoint: DeliveryCrashCheckpoint,
    identity: DeliveryCrashIdentity,
) {
    if (
        BuildConfig.DEBUG &&
        BuildConfig.APPLICATION_ID == "com.cheby.codex.mobile.gate" &&
        BuildConfig.RELAY_SERVICE_PORT == 27462
    ) {
        reached(checkpoint, identity)
    }
}
