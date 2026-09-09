package com.cheby.codex.mobile.gateway

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.cheby.codex.mobile.BuildConfig
import com.cheby.codex.mobile.data.AndroidKeystoreRelayConfigStore
import java.net.HttpURLConnection
import java.net.URI
import javax.net.ssl.HttpsURLConnection
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import kotlinx.coroutines.runBlocking

/** Opt-in device-side staging diagnostics; credentialed probes never log credential values. */
@RunWith(AndroidJUnit4::class)
class RelayNetworkPathInstrumentedTest {
    @Test(timeout = 60_000)
    fun existingRelaySessionCanOpenAuthenticatedWebSocket() {
        val arguments = InstrumentationRegistry.getArguments()
        assumeTrue(arguments.getString(ENABLE_ARGUMENT) == "true")
        val context = InstrumentationRegistry.getInstrumentation().targetContext.applicationContext
        val store = AndroidKeystoreRelayConfigStore(context)
        val current = requireNotNull(store.loadSession())
        val trust = RelayTlsTrust.load(context)
        assertExactBuildOrigin(current.relayOrigin)
        trust.requireAllowedOrigin(current.relayOrigin)
        val key = AndroidDeviceIdentity.loadExistingRelay()
        val endpoint = RelayEndpoint.parse(
            current.relayOrigin.replaceFirst("https://", "wss://") +
                RelayEndpoint.DEVICE_SOCKET_PATH,
        )
        val socket = RelayBoundedDuplexConnection(trust.socketFactory)

        try {
            socket.connect(endpoint, current.accessToken, key)
            val event = requireNotNull(socket.read() as? BoundedSocketEvent.Text) {
                "Relay WebSocket did not return a ready text frame"
            }
            val ready = requireNotNull(
                RelayV1Codec().decode(event.value) as? RelayV1Frame.Ready,
            ) { "Relay WebSocket returned an invalid ready frame" }
            assertEquals(current.assistantId, ready.assistantId)
            assertEquals(current.principalId, ready.principalId)
            assertTrue("Turkey Connector is not online for the paired assistant", ready.nodeOnline)
        } catch (handshake: WebSocketHandshakeException) {
            fail("Relay WebSocket handshake failed with HTTP ${handshake.statusCode}")
        } catch (error: Exception) {
            fail("Relay WebSocket failed safely: ${error::class.java.simpleName}: ${error.message}")
        } finally {
            socket.cancel()
        }
    }

    @Test
    fun existingRelaySessionCanRefresh() = runBlocking {
        val arguments = InstrumentationRegistry.getArguments()
        assumeTrue(arguments.getString(ENABLE_ARGUMENT) == "true")
        assumeTrue(arguments.getString(STATEFUL_REFRESH_ARGUMENT) == "true")
        val context = InstrumentationRegistry.getInstrumentation().targetContext.applicationContext
        val store = AndroidKeystoreRelayConfigStore(context)
        val current = requireNotNull(store.loadSession())
        val trust = RelayTlsTrust.load(context)
        assertExactBuildOrigin(current.relayOrigin)
        val key = AndroidDeviceIdentity.loadExistingRelay()

        val refreshed = try {
            RelayCredentialClient(
                client = trust.newHttpClient(),
                originValidator = trust::requireAllowedOrigin,
            ).refresh(current, key, store)
        } catch (request: GatewayRequestException) {
            fail("Relay refresh was rejected: ${request.code}")
            error("unreachable")
        } catch (error: Exception) {
            fail("Relay refresh failed safely: ${error::class.java.simpleName}: ${error.message}")
            error("unreachable")
        }

        assertEquals(current.bindingScope, refreshed.bindingScope)
        assertEquals(current.principalId, refreshed.principalId)
        assertTrue(refreshed.accessExpiresAt >= current.accessExpiresAt)
        assertTrue(
            "Relay refresh did not rotate both credentials",
            refreshed.accessToken != current.accessToken &&
                refreshed.refreshToken != current.refreshToken,
        )
    }

    @Test
    fun existingRelayIdentityCanSignProof() {
        val arguments = InstrumentationRegistry.getArguments()
        assumeTrue(arguments.getString(ENABLE_ARGUMENT) == "true")

        val key = AndroidDeviceIdentity.loadExistingRelay()
        val proof = DeviceProofV1.sign(
            key = key,
            method = "GET",
            rawTarget = "/relay/v1/device".toByteArray(Charsets.US_ASCII),
            body = DeviceProofV1.emptyBody(),
            bearerToken = "diagnostic-noncredential",
        )

        assertEquals(DeviceProofV1.VERSION, proof.version)
        assertTrue(proof.signature.isNotBlank())
    }

    @Test
    fun deviceNetworkCanReachRelayHealth() {
        val arguments = InstrumentationRegistry.getArguments()
        assumeTrue(arguments.getString(ENABLE_ARGUMENT) == "true")
        val origin = requireNotNull(arguments.getString(ORIGIN_ARGUMENT))
        assertExactBuildOrigin(origin)
        val context = InstrumentationRegistry.getInstrumentation().targetContext.applicationContext
        val trust = RelayTlsTrust.load(context)
        trust.requireAllowedOrigin(origin)
        val uri = URI(origin).normalize()
        require(
            uri.scheme == "https" &&
                !uri.host.isNullOrBlank() &&
                uri.rawUserInfo == null &&
                uri.rawQuery == null &&
                uri.rawFragment == null,
        ) { "A credential-free HTTPS Relay origin is required" }

        val healthUri = uri.resolve("/healthz")
        val connection = healthUri.toURL().openConnection() as HttpsURLConnection
        try {
            connection.sslSocketFactory = trust.socketFactory
            connection.instanceFollowRedirects = false
            connection.connectTimeout = 15_000
            connection.readTimeout = 15_000
            connection.requestMethod = "GET"
            assertEquals(HttpURLConnection.HTTP_OK, connection.responseCode)
            val body = connection.inputStream.bufferedReader(Charsets.UTF_8).use { reader ->
                val buffer = CharArray(MAX_HEALTH_BODY_CHARS + 1)
                var count = 0
                while (count < buffer.size) {
                    val read = reader.read(buffer, count, buffer.size - count)
                    if (read < 0) break
                    count += read
                }
                assertTrue(
                    "Relay health response exceeded the diagnostic limit",
                    count <= MAX_HEALTH_BODY_CHARS,
                )
                String(buffer, 0, count)
            }
            assertEquals("{\"status\":\"ok\"}", body)
        } finally {
            connection.disconnect()
        }
    }

    private fun assertExactBuildOrigin(origin: String) {
        val expected = "https://192.0.0.8:${BuildConfig.RELAY_SERVICE_PORT}"
        assertEquals(
            "This APK must probe only its build-authorized Relay port",
            expected,
            RelayOrigin.normalize(origin),
        )
        assertTrue(BuildConfig.RELAY_SERVICE_PORT == 27_461 || BuildConfig.RELAY_SERVICE_PORT == 27_462)
    }

    private companion object {
        const val ENABLE_ARGUMENT = "cheby_relay_network_probe"
        const val STATEFUL_REFRESH_ARGUMENT = "cheby_relay_stateful_refresh_probe"
        const val ORIGIN_ARGUMENT = "cheby_relay_origin"
        const val MAX_HEALTH_BODY_CHARS = 128
    }
}
