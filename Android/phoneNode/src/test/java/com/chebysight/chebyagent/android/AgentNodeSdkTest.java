package com.chebysight.chebyagent.android;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.util.Collections;
import java.util.HashSet;

public final class AgentNodeSdkTest {
    @Test
    public void systemSettingsUsesTheStablePlatformLaunchPath() {
        assertTrue(AgentNodeSdk.isSystemSettingsPackage("com.android.settings"));
        assertFalse(AgentNodeSdk.isSystemSettingsPackage("com.android.chrome"));
        assertFalse(AgentNodeSdk.isSystemSettingsPackage(""));
    }

    @Test
    public void launcherFallbackRequiresOwnExactLabelAndLauncherPackage() {
        String applicationLabel = "ChebyCodex";
        assertTrue(AgentAccessibilityService.trustedLauncherNode(
                "com.huawei.android.launcher",
                "com.huawei.android.launcher",
                applicationLabel,
                "",
                applicationLabel
        ));
        assertTrue(AgentAccessibilityService.trustedLauncherNode(
                "com.huawei.android.launcher",
                "com.huawei.android.launcher",
                "",
                applicationLabel,
                applicationLabel
        ));
        assertFalse(AgentAccessibilityService.trustedLauncherNode(
                "com.huawei.android.launcher",
                "com.example.spoof",
                applicationLabel,
                applicationLabel,
                applicationLabel
        ));
        assertFalse(AgentAccessibilityService.trustedLauncherNode(
                "com.huawei.android.launcher",
                "com.huawei.android.launcher",
                applicationLabel + " Beta",
                "",
                applicationLabel
        ));
        assertFalse(AgentAccessibilityService.trustedLauncherNode(
                "",
                "com.huawei.android.launcher",
                applicationLabel,
                "",
                applicationLabel
        ));
        assertFalse(AgentAccessibilityService.trustedLauncherNode(
                "com.huawei.android.launcher",
                "com.huawei.android.launcher",
                applicationLabel,
                "",
                "ChebyNode Embedded"
        ));
    }

    @Test
    public void urlResolverPinsOnlyOneDistinctHandlerPackage() {
        assertTrue(AgentNodeSdk.singleDistinctPackage(
                Collections.singleton("com.UCMobile")
        ).equals("com.UCMobile"));
        HashSet<String> several = new HashSet<>();
        several.add("com.UCMobile");
        several.add("com.example.browser");
        assertTrue(AgentNodeSdk.singleDistinctPackage(several).isEmpty());
        assertTrue(AgentNodeSdk.singleDistinctPackage(Collections.emptySet()).isEmpty());
    }

    @Test
    public void resolverIsNotReportedAsTheOpenedBrowser() {
        assertTrue(AgentNodeSdk.isResolverComponent(
                "com.huawei.android.internal.app",
                "com.android.internal.app.ResolverActivity"
        ));
        assertFalse(AgentNodeSdk.isResolverComponent(
                "com.UCMobile",
                "com.uc.browser.InnerUCMobile"
        ));
    }

    @Test
    public void onlyOneTargetLaunchCanOwnTheGate() {
        AgentNodeSdk.endTargetLaunch();
        assertTrue(AgentNodeSdk.tryBeginTargetLaunch());
        assertFalse(AgentNodeSdk.tryBeginTargetLaunch());
        AgentNodeSdk.endTargetLaunch();
        assertTrue(AgentNodeSdk.tryBeginTargetLaunch());
        AgentNodeSdk.endTargetLaunch();
    }
}
