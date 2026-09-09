package com.cheby.codex.mobile.gateway

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

class RelayTrustBundleCrossContractTest {
    @Test
    fun `production generator fixture authorizes only production port`() {
        val trust = RelayTlsTrust.fromBundle(
            raw = fixture("relay-trust-bundle.production.json").readText(),
            allowPlatformFallback = false,
            authorizedPort = 27_461,
        )

        trust.requireAllowedOrigin("https://192.0.0.9:27461")
        trust.requireAllowedOrigin("https://192.0.0.8:27461")
        assertRejected { trust.requireAllowedOrigin("https://192.0.0.9:27462") }
        assertRejected { trust.requireAllowedOrigin("https://relay.example:27461") }
        assertRejected { trust.requireAllowedOrigin("https://192.168.0.10:27461") }
    }

    @Test
    fun `gate generator fixture authorizes only gate port`() {
        val trust = RelayTlsTrust.fromBundle(
            raw = fixture("relay-trust-bundle.gate.json").readText(),
            allowPlatformFallback = false,
            authorizedPort = 27_462,
        )

        trust.requireAllowedOrigin("https://192.0.0.9:27462")
        assertRejected { trust.requireAllowedOrigin("https://192.0.0.9:27461") }
    }

    @Test
    fun `bundle from one build cannot be relabeled for the other build`() {
        assertRejected {
            RelayTlsTrust.fromBundle(
                raw = fixture("relay-trust-bundle.production.json").readText(),
                allowPlatformFallback = false,
                authorizedPort = 27_462,
            )
        }
        assertRejected {
            RelayTlsTrust.fromBundle(
                raw = fixture("relay-trust-bundle.gate.json").readText(),
                allowPlatformFallback = false,
                authorizedPort = 27_461,
            )
        }
    }

    @Test
    fun `signed bundle supplies migration target without restricting configurable public IP`() {
        val raw = fixture("relay-trust-bundle.production.json").readText()
            .replace("192.0.0.9", "192.0.0.8")
        val trust = RelayTlsTrust.fromBundle(
            raw = raw,
            allowPlatformFallback = false,
            authorizedPort = 27_461,
        )

        assertTrue(trust.authorizedOrigin == "https://192.0.0.8:27461")
        trust.requireAllowedOrigin("https://192.0.0.8:27461")
        trust.requireAllowedOrigin("https://192.0.0.9:27461")
        assertRejected { trust.requireAllowedOrigin("https://192.0.0.8:27462") }
    }

    @Test
    fun `bundle rejects private reserved and noncanonical IPv4 identities`() {
        val raw = fixture("relay-trust-bundle.production.json").readText()
        listOf("10.0.0.1", "172.16.0.1", "192.168.0.1", "127.0.0.1", "213.250.148.0166")
            .forEach { invalidIp ->
                assertRejected {
                    RelayTlsTrust.fromBundle(
                        raw = raw.replace("192.0.0.9", invalidIp),
                        allowPlatformFallback = false,
                        authorizedPort = 27_461,
                    )
                }
            }
    }

    private fun fixture(name: String): File {
        val relative = "deploy/relay-edge/tests/fixtures/$name"
        val workingDirectory = System.getProperty("user.dir")
            ?: error("Missing user.dir while resolving cross-contract fixture")
        return generateSequence(File(workingDirectory).absoluteFile) { it.parentFile }
            .map { File(it, relative) }
            .firstOrNull(File::isFile)
            ?: error("Missing cross-contract fixture: $relative")
    }

    private fun assertRejected(block: () -> Unit) {
        assertTrue(runCatching(block).exceptionOrNull() is GatewayTransportException)
    }
}
