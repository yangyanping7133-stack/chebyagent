package com.cheby.codex.mobile.runtime

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import com.cheby.codex.runtime.EmbeddedRuntimeRegistry
import com.cheby.codex.runtime.EmbeddedRuntimeStartResult

internal enum class StandaloneRuntimeStartResult {
    REQUESTED,
    PERMISSION_REQUIRED,
    TERMUX_UNAVAILABLE,
}

/** Starts only the pinned on-device runtime entrypoint. No arbitrary command input is accepted. */
internal class StandaloneRuntimeSupervisor(
    private val context: Context,
) {
    fun ensureStarted(tokenSha256: String): StandaloneRuntimeStartResult {
        require(tokenSha256.matches(SHA256_HEX))
        when (EmbeddedRuntimeRegistry.ensureStarted(tokenSha256)) {
            EmbeddedRuntimeStartResult.REQUESTED,
            EmbeddedRuntimeStartResult.PREPARING,
            -> return StandaloneRuntimeStartResult.REQUESTED
            EmbeddedRuntimeStartResult.FAILED ->
                return StandaloneRuntimeStartResult.PERMISSION_REQUIRED
            EmbeddedRuntimeStartResult.UNAVAILABLE -> Unit
        }
        if (context.packageManager.getApplicationInfoOrNull(TERMUX_PACKAGE) == null) {
            return StandaloneRuntimeStartResult.TERMUX_UNAVAILABLE
        }
        if (
            context.checkSelfPermission(TERMUX_RUN_COMMAND_PERMISSION) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            return StandaloneRuntimeStartResult.PERMISSION_REQUIRED
        }
        val intent = Intent(TERMUX_RUN_COMMAND_ACTION).apply {
            component = ComponentName(TERMUX_PACKAGE, TERMUX_RUN_COMMAND_SERVICE)
            putExtra(TERMUX_RUN_COMMAND_PATH, PROOT_DISTRO_PATH)
            putExtra(
                TERMUX_RUN_COMMAND_ARGUMENTS,
                arrayOf(
                    "login",
                    "debian",
                    "--",
                    START_SCRIPT,
                    "--token-sha256",
                    tokenSha256,
                    "--foreground",
                ),
            )
            putExtra(TERMUX_RUN_COMMAND_WORKDIR, TERMUX_HOME)
            putExtra(TERMUX_RUN_COMMAND_BACKGROUND, true)
            putExtra(TERMUX_RUN_COMMAND_LABEL, "ChebyCodex runtime")
        }
        return runCatching {
            context.startService(intent)
            StandaloneRuntimeStartResult.REQUESTED
        }.getOrDefault(StandaloneRuntimeStartResult.PERMISSION_REQUIRED)
    }

    private fun PackageManager.getApplicationInfoOrNull(packageName: String) =
        runCatching { getApplicationInfo(packageName, 0) }.getOrNull()

    private companion object {
        val SHA256_HEX = Regex("[0-9a-f]{64}")
        const val TERMUX_PACKAGE = "com.termux"
        const val TERMUX_RUN_COMMAND_PERMISSION = "com.termux.permission.RUN_COMMAND"
        const val TERMUX_RUN_COMMAND_ACTION = "com.termux.RUN_COMMAND"
        const val TERMUX_RUN_COMMAND_SERVICE = "com.termux.app.RunCommandService"
        const val TERMUX_RUN_COMMAND_PATH = "com.termux.RUN_COMMAND_PATH"
        const val TERMUX_RUN_COMMAND_ARGUMENTS = "com.termux.RUN_COMMAND_ARGUMENTS"
        const val TERMUX_RUN_COMMAND_WORKDIR = "com.termux.RUN_COMMAND_WORKDIR"
        const val TERMUX_RUN_COMMAND_BACKGROUND = "com.termux.RUN_COMMAND_BACKGROUND"
        const val TERMUX_RUN_COMMAND_LABEL = "com.termux.RUN_COMMAND_COMMAND_LABEL"
        const val PROOT_DISTRO_PATH = "/data/data/com.termux/files/usr/bin/proot-distro"
        const val TERMUX_HOME = "/data/data/com.termux/files/home"
        const val START_SCRIPT = "/opt/cheby/bin/start-codex-appserver"
    }
}
