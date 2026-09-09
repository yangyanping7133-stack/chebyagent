package com.cheby.codex.runtime;

/** Coarse, secret-free state intended for first-run product UI. */
public enum EmbeddedRuntimeStatus {
    UNAVAILABLE,
    PREPARING,
    INSTALLING,
    READY_NEEDS_AUTH,
    READY,
    FAILED,
}
