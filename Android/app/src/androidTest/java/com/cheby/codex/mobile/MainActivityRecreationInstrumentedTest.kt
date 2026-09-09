package com.cheby.codex.mobile

import android.content.ComponentName
import android.content.pm.ActivityInfo
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.cheby.codex.mobile.gateway.GatewayFactory
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class MainActivityRecreationInstrumentedTest {
    @Test
    fun launcherUsesOneTaskLevelMainActivityInstance() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val activityInfo = context.packageManager.getActivityInfo(
            ComponentName(context, MainActivity::class.java),
            0,
        )

        assertEquals(ActivityInfo.LAUNCH_SINGLE_TASK, activityInfo.launchMode)
    }

    @Test
    fun realMainActivityRecreationDoesNotConstructASecondGatewayDependencySet() {
        val gatewayCreationsBefore = GatewayFactory.creationCountForTests()
        val dependencyCreationsBefore = MainActivityViewModelFactory.creationCountForTests()
        val scenario = ActivityScenario.launch(MainActivity::class.java)
        try {
            awaitCount(gatewayCreationsBefore + 1) { GatewayFactory.creationCountForTests() }
            awaitCount(dependencyCreationsBefore + 1) {
                MainActivityViewModelFactory.creationCountForTests()
            }

            scenario.recreate()
            InstrumentationRegistry.getInstrumentation().waitForIdleSync()
            Thread.sleep(250)

            assertEquals(gatewayCreationsBefore + 1, GatewayFactory.creationCountForTests())
            assertEquals(
                dependencyCreationsBefore + 1,
                MainActivityViewModelFactory.creationCountForTests(),
            )
        } finally {
            scenario.close()
        }
    }

    private fun awaitCount(expected: Long, current: () -> Long) {
        repeat(150) {
            if (current() == expected) return
            Thread.sleep(20)
        }
        assertTrue("Timed out waiting for production dependency construction", current() == expected)
    }
}
