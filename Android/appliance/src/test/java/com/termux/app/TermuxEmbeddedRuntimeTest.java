package com.termux.app;

import static org.junit.Assert.assertEquals;

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
}
