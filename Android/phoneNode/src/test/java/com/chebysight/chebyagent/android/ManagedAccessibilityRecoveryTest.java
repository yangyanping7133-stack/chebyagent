package com.chebysight.chebyagent.android;

import org.junit.Test;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;
import static org.junit.Assert.*;

public class ManagedAccessibilityRecoveryTest {
    private static final class Fake implements ManagedAccessibilityRecovery.Driver,
            ManagedAccessibilityRecovery.Scheduler {
        boolean permitted = true, enabled, bound, enableAccepted = true, detachAccepted = true;
        int enables, detaches;
        String phase;
        boolean reportedReady;
        final ArrayDeque<Runnable> queue = new ArrayDeque<>();
        final List<Long> delays = new ArrayList<>();
        final ManagedAccessibilityRecovery recovery = new ManagedAccessibilityRecovery(this, this);
        public boolean permitted() { return permitted; }
        public boolean enabled() { return enabled; }
        public boolean bound() { return bound; }
        public boolean enable() { enables++; if (enableAccepted) enabled = true; return enableAccepted; }
        public boolean removeOwnService() { detaches++; if (detachAccepted) enabled = false; return detachAccepted; }
        public void report(String p, boolean e, boolean b) { phase = p; reportedReady = e && b; }
        public void post(Runnable task, long delayMillis) { queue.add(task); delays.add(delayMillis); }
        public void cancel(Runnable task) { queue.remove(task); }
        void next() { queue.remove().run(); }
    }

    @Test public void readyServiceIsNotToggled() {
        Fake f = new Fake(); f.enabled = f.bound = true;
        f.recovery.request();
        assertEquals(0, f.enables); assertEquals(0, f.detaches);
        assertTrue(f.reportedReady); assertTrue(f.queue.isEmpty());
    }

    @Test public void noPermissionMeansNoSettingsWrites() {
        Fake f = new Fake(); f.permitted = false;
        f.recovery.request();
        assertEquals(0, f.enables); assertEquals(0, f.detaches);
        assertEquals("permission_missing", f.phase); assertTrue(f.queue.isEmpty());
    }

    @Test public void normalBindingGetsAChanceAndRepeatedLaunchesCoalesce() {
        Fake f = new Fake(); f.recovery.request(); f.recovery.request();
        assertEquals(1, f.enables); assertEquals(1, f.queue.size());
        assertEquals(Long.valueOf(1200), f.delays.get(0));
        f.bound = true; f.next();
        assertTrue(f.reportedReady); assertEquals(0, f.detaches);
    }

    @Test public void rebindUsesSeparateObserverTurnsAndVerifiesActualBinding() {
        Fake f = new Fake(); f.recovery.request(); f.next();
        assertEquals(1, f.detaches); assertFalse(f.enabled);
        assertEquals(1, f.enables); assertEquals(Long.valueOf(250), f.delays.get(1));
        f.next(); assertEquals(2, f.enables); assertTrue(f.enabled);
        f.bound = true; f.next();
        assertTrue(f.reportedReady); assertEquals("verified", f.phase); assertTrue(f.queue.isEmpty());
    }

    @Test public void successfulSettingsWritesDoNotPretendBindingSucceededOrLoop() {
        Fake f = new Fake(); f.recovery.request(); f.next(); f.next(); f.next();
        assertFalse(f.reportedReady); assertEquals(1, f.detaches);
        assertEquals(2, f.enables); assertTrue(f.queue.isEmpty());
    }

    @Test public void rejectedEnableDoesNotDetach() {
        Fake f = new Fake(); f.enableAccepted = false; f.recovery.request();
        assertEquals(0, f.detaches); assertEquals("enable_rejected", f.phase);
        assertTrue(f.queue.isEmpty());
    }

    @Test public void rejectedDetachDoesNotScheduleAnotherWrite() {
        Fake f = new Fake(); f.detachAccepted = false; f.recovery.request(); f.next();
        assertEquals(1, f.enables); assertEquals("detach_rejected", f.phase);
        assertTrue(f.queue.isEmpty());
    }

    @Test public void permissionLossBeforeDetachStopsRecovery() {
        Fake f = new Fake(); f.recovery.request(); f.permitted = false; f.next();
        assertEquals(0, f.detaches); assertTrue(f.queue.isEmpty());
    }

    @Test public void closeBeforeDetachCancelsWithoutWrites() {
        Fake f = new Fake(); f.recovery.request(); f.recovery.close(); f.recovery.request();
        assertEquals(1, f.enables); assertEquals(0, f.detaches); assertTrue(f.queue.isEmpty());
    }

    @Test public void closeDuringDetachRestoresOwnServiceAndCancelsDelayedWork() {
        Fake f = new Fake(); f.recovery.request(); f.next(); f.recovery.close();
        assertTrue(f.enabled); assertEquals(2, f.enables); assertTrue(f.queue.isEmpty());
        f.recovery.request(); assertEquals(2, f.enables);
    }

    @Test public void permissionRevocationAfterDetachIsNotBypassedOnClose() {
        Fake f = new Fake(); f.recovery.request(); f.next(); f.permitted = false;
        f.next(); f.recovery.close();
        assertEquals(1, f.enables); assertEquals("permission_missing", f.phase);
        assertTrue(f.queue.isEmpty());
    }

    @Test public void failedRestoreCanBeRepairedOnCloseWithoutAnInfiniteRetry() {
        Fake f = new Fake(); f.recovery.request(); f.next(); f.enableAccepted = false; f.next();
        assertEquals("restore_rejected", f.phase); assertFalse(f.reportedReady);
        assertTrue(f.queue.isEmpty()); f.enableAccepted = true; f.recovery.close();
        assertTrue(f.enabled); assertEquals(3, f.enables); assertTrue(f.queue.isEmpty());
    }

    @Test public void repeatedCloseDoesNotKeepRetryingRejectedRestore() {
        Fake f = new Fake(); f.recovery.request(); f.next(); f.enableAccepted = false;
        f.recovery.close(); f.recovery.close();
        assertEquals(2, f.enables); assertTrue(f.queue.isEmpty());
    }
}
