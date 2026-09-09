package com.cheby.codex.mobile

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class StandaloneBuildIdentityTest {
    @Test
    fun standaloneBuildCannotReuseDistributedIdentityOrRelayFallbacks() {
        assertEquals("com.cheby.codex.mobile.standalone", BuildConfig.APPLICATION_ID)
        assertTrue(BuildConfig.STANDALONE_MODE)
        assertFalse(BuildConfig.DEMO_GATEWAY_ENABLED)
        assertFalse(BuildConfig.LEGACY_RELAY_MIGRATION_ENABLED)
        assertEquals("", BuildConfig.LEGACY_RELAY_ORIGIN)
    }
}
