package com.chebysight.chebyagent.android;

/** One bounded recovery per launch request on an explicitly managed device. */
final class ManagedAccessibilityRecovery {
    interface Driver {
        boolean permitted();
        boolean enabled();
        boolean bound();
        boolean enable();
        boolean removeOwnService();
        void report(String phase, boolean enabled, boolean bound);
    }

    interface Scheduler {
        void post(Runnable task, long delayMillis);
        void cancel(Runnable task);
    }

    private final Driver driver;
    private final Scheduler scheduler;
    private boolean pending;
    private boolean removed;
    private boolean closed;
    private final Runnable assess = this::assessBinding;
    private final Runnable restore = this::restoreBinding;
    private final Runnable verify = () -> finish("verified");

    ManagedAccessibilityRecovery(Driver driver, Scheduler scheduler) {
        this.driver = driver;
        this.scheduler = scheduler;
    }

    void request() {
        if (closed || pending) return;
        if (ready()) { finish("already_ready"); return; }
        if (!driver.permitted()) { finish("permission_missing"); return; }
        pending = true;
        if (!driver.enable()) { finish("enable_rejected"); return; }
        // Give the platform time to bind normally before toggling our own component.
        scheduler.post(assess, 1200);
    }

    private boolean ready() {
        return driver.enabled() && driver.bound();
    }

    private void assessBinding() {
        if (closed) return;
        if (ready()) { finish("normal_bind"); return; }
        if (!driver.permitted()) { finish("permission_missing"); return; }
        if (!driver.removeOwnService()) { finish("detach_rejected"); return; }
        removed = true;
        // Separate Settings observer turns; consecutive identical writes may not rebind.
        scheduler.post(restore, 250);
    }

    private void restoreBinding() {
        if (closed) return;
        if (!driver.permitted()) { finish("permission_missing"); return; }
        removed = !driver.enable();
        if (removed) { finish("restore_rejected"); return; }
        scheduler.post(verify, 1200);
    }

    private void finish(String phase) {
        pending = false;
        driver.report(phase, driver.enabled(), driver.bound());
    }

    void close() {
        if (closed) return;
        closed = true;
        scheduler.cancel(assess);
        scheduler.cancel(restore);
        scheduler.cancel(verify);
        // Do not leave our temporary removal behind when the service is destroyed.
        if (removed && driver.permitted()) {
            removed = !driver.enable();
            finish("closed_restore");
        }
    }
}
