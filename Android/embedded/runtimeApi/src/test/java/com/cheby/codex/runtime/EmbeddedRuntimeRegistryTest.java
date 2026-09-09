package com.cheby.codex.runtime;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

import android.app.Activity;

import org.junit.Test;

public final class EmbeddedRuntimeRegistryTest {
    @Test
    public void routesStartRequestsToTheSingleInstalledOwner() {
        assertEquals(
            EmbeddedRuntimeStartResult.UNAVAILABLE,
            EmbeddedRuntimeRegistry.ensureStarted(repeat("0", 64))
        );
        assertEquals(EmbeddedRuntimeStatus.UNAVAILABLE, EmbeddedRuntimeRegistry.status());

        EmbeddedRuntime owner = new EmbeddedRuntime() {
            @Override
            public void prepare(Activity activity) {}

            @Override
            public EmbeddedRuntimeStartResult ensureStarted(String tokenSha256) {
                return repeat("a", 64).equals(tokenSha256)
                    ? EmbeddedRuntimeStartResult.REQUESTED
                    : EmbeddedRuntimeStartResult.FAILED;
            }

            @Override
            public EmbeddedRuntimeStatus status() {
                return EmbeddedRuntimeStatus.INSTALLING;
            }
        };
        EmbeddedRuntimeRegistry.install(owner);

        assertEquals(
            EmbeddedRuntimeStartResult.REQUESTED,
            EmbeddedRuntimeRegistry.ensureStarted(repeat("a", 64))
        );
        assertEquals(EmbeddedRuntimeStatus.INSTALLING, EmbeddedRuntimeRegistry.status());
        assertThrows(
            IllegalStateException.class,
            () -> EmbeddedRuntimeRegistry.install(owner)
        );
    }

    private static String repeat(String value, int count) {
        StringBuilder result = new StringBuilder(value.length() * count);
        for (int index = 0; index < count; index++) result.append(value);
        return result.toString();
    }
}
