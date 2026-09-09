package com.chebysight.chebyagent.android;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

public class AccessibilityServiceListTest {
    @Test
    public void addsServiceWithoutRemovingExistingEntries() {
        assertEquals(
                "one/Service:two/Service:cheby/Agent",
                AccessibilityServiceList.add("one/Service:two/Service", "cheby/Agent")
        );
    }

    @Test
    public void doesNotDuplicateEquivalentShortComponent() {
        assertEquals(
                "one/Service:cheby/.Agent",
                AccessibilityServiceList.add("one/Service:cheby/.Agent", "cheby/cheby.Agent")
        );
    }

    @Test
    public void preservesCaseDistinctComponentAndAddsRequestedService() {
        assertEquals(
                "one/Service:cheby/Agent:CHEBY/AGENT",
                AccessibilityServiceList.add("one/Service:cheby/Agent", "CHEBY/AGENT")
        );
    }

    @Test
    public void preservesExistingOrderAndDuplicates() {
        assertEquals(
                "one/Service:one/Service:cheby/Agent",
                AccessibilityServiceList.add("one/Service:one/Service", "cheby/Agent")
        );
    }

    @Test
    public void rejectsMissingRequestedService() {
        assertThrows(IllegalArgumentException.class, () -> AccessibilityServiceList.add("one/Service", " "));
    }

    @Test
    public void removesOnlyOwnEquivalentComponentsPreservingOtherServices() {
        assertEquals("one/Service:one/Service:CHEBY/Agent:two/Service",
                AccessibilityServiceList.remove(
                        "one/Service:cheby/.Agent:one/Service:CHEBY/Agent:cheby/cheby.Agent:two/Service",
                        "cheby/cheby.Agent"));
    }

    @Test
    public void removalAndRestorePreserveServicesAddedBetweenObserverTurns() {
        String detached = AccessibilityServiceList.remove("one/Service:cheby/.Agent", "cheby/cheby.Agent");
        assertEquals("one/Service:new/Service:cheby/cheby.Agent",
                AccessibilityServiceList.add(detached + ":new/Service", "cheby/cheby.Agent"));
    }

    @Test
    public void removingAbsentServiceDoesNotAddIt() {
        assertEquals("one/Service", AccessibilityServiceList.remove("one/Service", "cheby/Agent"));
        assertEquals("", AccessibilityServiceList.remove(null, "cheby/Agent"));
        assertThrows(IllegalArgumentException.class, () -> AccessibilityServiceList.remove("one/Service", ""));
    }
}
