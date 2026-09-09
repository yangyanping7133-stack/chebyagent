package com.cheby.codex.mobile.gateway

import android.system.Os
import android.system.OsConstants
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.cheby.codex.mobile.BuildConfig
import java.io.File
import java.io.FileInputStream
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import kotlinx.coroutines.runBlocking
import org.junit.Assert.fail
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * One-time, opt-in Gate provisioning from a fixed app-private file.
 *
 * The host may stream a local mode-0600 CXC1 invitation into [INPUT_FILE_NAME] with `run-as`.
 * Neither the invitation nor its source path is accepted as an instrumentation argument. The file
 * is closed and deleted before any enrollment network request begins.
 */
@RunWith(AndroidJUnit4::class)
class RelayGateProvisioningInstrumentedTest {
    @Test
    fun consumesPrivateInvitationAndPersistsRelaySession() = runBlocking {
        val arguments = InstrumentationRegistry.getArguments()
        assumeTrue(
            "Gate provisioning is disabled unless $ENABLE_ARGUMENT=true is supplied",
            arguments.getString(ENABLE_ARGUMENT) == "true",
        )
        var gateway: CodexGateway? = null
        try {
            require(
                BuildConfig.DEBUG &&
                    BuildConfig.RELAY_PRIVATE_CA_REQUIRED &&
                    BuildConfig.RELAY_SERVICE_PORT == GATE_PORT,
            )
            val context =
                InstrumentationRegistry.getInstrumentation().targetContext.applicationContext
            val invitation = RelayInvitationCodec.decode(consumePrivateInvitation(context.filesDir))
            val createdGateway = GatewayFactory.create(context)
            gateway = createdGateway
            val controls = createdGateway as? AssistantEnrollmentControls
                ?: error("Gate enrollment controls are unavailable")
            controls.connect(GATE_ORIGIN, invitation.assistantId, invitation.pairingSecret)
        } catch (error: Exception) {
            fail("Gate provisioning failed safely: ${error::class.java.simpleName}")
        } finally {
            (gateway as? CloseableCodexGateway)?.close()
        }
    }

    private fun consumePrivateInvitation(filesDirectory: File): String {
        val privateRoot = filesDirectory.canonicalFile
        val invitationFile = privateRoot.resolve(INPUT_FILE_NAME)
        require(invitationFile.parentFile?.canonicalFile == privateRoot) {
            "Provisioning input location is invalid"
        }
        val bytes = try {
            val descriptor = Os.open(
                invitationFile.absolutePath,
                OsConstants.O_RDONLY or OsConstants.O_NOFOLLOW,
                0,
            )
            FileInputStream(descriptor).use { input ->
                val status = Os.fstat(descriptor)
                require(OsConstants.S_ISREG(status.st_mode)) {
                    "Provisioning input must be a regular file"
                }
                val permissions = status.st_mode and PERMISSION_MASK
                require(permissions == PRIVATE_FILE_PERMISSIONS) {
                    "Provisioning input must have mode 0600"
                }
                val bounded = ByteArray(MAX_INVITATION_BYTES + 1)
                var count = 0
                while (count < bounded.size) {
                    val read = input.read(bounded, count, bounded.size - count)
                    if (read < 0) break
                    count += read
                }
                require(count in MIN_INVITATION_BYTES..MAX_INVITATION_BYTES) {
                    "Provisioning input length is invalid"
                }
                bounded.copyOf(count).also { bounded.fill(0) }
            }
        } finally {
            require(invitationFile.delete() || !invitationFile.exists()) {
                "Provisioning input could not be deleted"
            }
        }

        return try {
            Charsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(bytes))
                .toString()
                .also { value ->
                    require(value.startsWith(INVITATION_PREFIX)) {
                        "Provisioning input is not a CXC1 invitation"
                    }
                    require(value.all { it.code in PRINTABLE_ASCII }) {
                        "Provisioning input contains invalid characters"
                    }
                }
        } finally {
            bytes.fill(0)
        }
    }

    private companion object {
        const val ENABLE_ARGUMENT = "cheby_relay_gate_provision"
        const val INPUT_FILE_NAME = "cheby-gate-enrollment.cxc1"
        const val GATE_PORT = 27_462
        const val GATE_ORIGIN = "https://192.0.0.8:27462"
        const val INVITATION_PREFIX = "CXC1."
        const val MIN_INVITATION_BYTES = INVITATION_PREFIX.length + 1
        const val MAX_INVITATION_BYTES = 1_024
        const val PERMISSION_MASK = 0x1ff
        val PRIVATE_FILE_PERMISSIONS = OsConstants.S_IRUSR or OsConstants.S_IWUSR
        val PRINTABLE_ASCII = 0x21..0x7e
    }
}
