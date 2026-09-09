package com.termux.app;

import com.cheby.codex.runtime.EmbeddedRuntimeRegistry;
import com.chebysight.chebyagent.android.AgentNodeLifecycleBridge;

/** Owns both the Termux lifecycle and the Cheby runtime adapter in the appliance process. */
public final class ChebyApplianceApplication extends TermuxApplication {
    @Override
    public void onCreate() {
        super.onCreate();
        AgentNodeLifecycleBridge.install(this);
        TermuxEmbeddedRuntime runtime = new TermuxEmbeddedRuntime(this);
        EmbeddedRuntimeRegistry.install(runtime);
        runtime.resumeIfProvisioned();
    }
}
