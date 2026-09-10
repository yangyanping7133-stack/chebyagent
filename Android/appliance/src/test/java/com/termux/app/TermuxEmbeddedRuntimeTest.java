package com.termux.app;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.util.HashSet;
import java.util.Set;

import org.junit.Test;

public final class TermuxEmbeddedRuntimeTest {
    @Test
    public void mapsOnlyKnownProvisionStepsToSafeUserFacingDetails() {
        assertEquals(
            "Debian 环境迁移失败",
            TermuxEmbeddedRuntime.failureDetailForStep("migrate_debian_rootfs")
        );
        assertEquals(
            "安装步骤未完成",
            TermuxEmbeddedRuntime.failureDetailForStep("private/path/or/error")
        );
    }

    @Test
    public void acceptsSignedAssetManifestAdditionsButRejectsMissingRequiredAssets() {
        Set<String> manifestNames = new HashSet<>(
            TermuxEmbeddedRuntime.requiredAssetNamesForTest()
        );
        manifestNames.add("future-notice.txt");
        assertTrue(TermuxEmbeddedRuntime.assetManifestContainsRequired(manifestNames));

        manifestNames.remove("NOTICE");
        assertFalse(TermuxEmbeddedRuntime.assetManifestContainsRequired(manifestNames));
    }
}
