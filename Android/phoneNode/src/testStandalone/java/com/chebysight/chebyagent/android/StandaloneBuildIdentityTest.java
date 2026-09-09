package com.chebysight.chebyagent.android;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public final class StandaloneBuildIdentityTest {
    @Test
    public void standaloneBuildUsesAnIsolatedApplicationIdentity() {
        assertEquals(
                "com.chebysight.chebyagent.phonenode.standalone",
                BuildConfig.APPLICATION_ID
        );
        assertTrue(BuildConfig.STANDALONE_MODE);
        assertEquals("ChebyNode Standalone", BuildConfig.AGENT_NODE_PRODUCT_NAME);
        assertEquals(
                "http://127.0.0.1:3448",
                BuildConfig.PHONEBRIDGE_DEFAULT_BASE_URL
        );
    }
}
