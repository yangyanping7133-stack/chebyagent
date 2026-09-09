package com.chebysight.chebyagent.android;

import org.junit.Test;

import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class AccessibilitySettingsTransactionTest {
    @Test
    public void doesNotEnableGlobalAccessibilityWhenServiceListWriteFails() {
        AtomicInteger enableCalls = new AtomicInteger();
        boolean result = AccessibilitySettingsTransaction.apply(
                new AccessibilitySettingsTransaction.Writer() {
                    @Override
                    public boolean writeEnabledServices(String value) {
                        return false;
                    }

                    @Override
                    public boolean enableAccessibility() {
                        enableCalls.incrementAndGet();
                        return true;
                    }
                },
                "cheby/Agent"
        );

        assertFalse(result);
        assertEquals(0, enableCalls.get());
    }

    @Test
    public void enablesGlobalAccessibilityOnlyAfterServiceListWriteSucceeds() {
        AtomicInteger enableCalls = new AtomicInteger();
        boolean result = AccessibilitySettingsTransaction.apply(
                new AccessibilitySettingsTransaction.Writer() {
                    @Override
                    public boolean writeEnabledServices(String value) {
                        return "cheby/Agent".equals(value);
                    }

                    @Override
                    public boolean enableAccessibility() {
                        enableCalls.incrementAndGet();
                        return true;
                    }
                },
                "cheby/Agent"
        );

        assertTrue(result);
        assertEquals(1, enableCalls.get());
    }
}
