package com.cheby.codex.mobile.gateway

import android.content.Context
import com.cheby.codex.mobile.BuildConfig
import java.io.ByteArrayInputStream
import java.net.URI
import java.security.KeyStore
import java.security.MessageDigest
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.util.Base64
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient

/**
 * Relay-only trust configuration loaded from an APK-signed asset.
 *
 * It never changes Android's global trust policy. Release builds trust only the private CA roots
 * listed in the bundle; debug builds may explicitly fall back to platform trust for local tests.
 */
internal class RelayTlsTrust private constructor(
    val socketFactory: SSLSocketFactory,
    private val trustManager: X509TrustManager,
    val authorizedOrigin: String?,
    private val authorizedPort: Int?,
) {
    fun newHttpClient(): OkHttpClient = defaultRelayHttpClientBuilder()
        .sslSocketFactory(socketFactory, trustManager)
        .build()

    fun requireAllowedOrigin(origin: String) {
        val port = authorizedPort ?: return
        val normalized = RelayOrigin.normalize(origin)
        val uri = URI(normalized)
        val accepted = runCatching {
            requirePublicIpv4(requireNotNull(uri.host))
            (if (uri.port == -1) 443 else uri.port) == port
        }.getOrDefault(false)
        if (!accepted) {
            throw GatewayTransportException("Relay address is not authorized by this APK")
        }
    }

    companion object {
        private const val ASSET_NAME = "relay-trust-bundle.json"
        private val HEX_SHA256 = Regex("^[0-9a-f]{64}$")
        private val AUTHORITY_ID = Regex("^[A-Za-z0-9._-]{1,64}$")
        private val json = Json { ignoreUnknownKeys = false }

        fun load(context: Context): RelayTlsTrust {
            val raw = runCatching {
                context.assets.open(ASSET_NAME).use { input ->
                    val bytes = input.readBytes()
                    require(bytes.size in 2..MAX_BUNDLE_BYTES)
                    bytes.toString(Charsets.UTF_8)
                }
            }.getOrNull()
            if (raw == null) {
                if (BuildConfig.RELAY_PRIVATE_CA_REQUIRED) {
                    throw GatewayTransportException("Relay trust bundle is missing")
                }
                return platformOnly()
            }
            return fromBundle(
                raw = raw,
                allowPlatformFallback = BuildConfig.RELAY_PLATFORM_TRUST_FALLBACK,
                authorizedPort = BuildConfig.RELAY_SERVICE_PORT,
            )
        }

        internal fun fromBundle(
            raw: String,
            allowPlatformFallback: Boolean,
            authorizedPort: Int = BuildConfig.RELAY_SERVICE_PORT,
            platformTrustManager: X509TrustManager? = null,
        ): RelayTlsTrust {
            val root = try {
                json.parseToJsonElement(raw).jsonObject
            } catch (_: Exception) {
                throw GatewayTransportException("Relay trust bundle is invalid")
            }
            try {
                require(root.keys == ROOT_FIELDS)
                require(root.getValue("version").jsonPrimitive.intOrNull == 1)
                val publicIp = requirePublicIpv4(root.requiredString("publicIp"))
                val ports = root.getValue("servicePorts").jsonArray.map {
                    it.jsonPrimitive.intOrNull ?: error("Invalid Relay port")
                }
                require(authorizedPort in setOf(27_461, 27_462))
                require(ports == listOf(authorizedPort))
                val authorities = root.getValue("authorities").jsonArray
                require(authorities.size in 1..2)
                val certificates = authorities.map { authorityElement ->
                    decodeAuthority(authorityElement.jsonObject)
                }
                require(certificates.map { it.first }.distinct().size == certificates.size)
                val privateTrustManager = trustManagerFor(certificates)
                val effectiveTrustManager = if (allowPlatformFallback) {
                    FallbackTrustManager(
                        privateTrustManager,
                        platformTrustManager ?: platformTrustManager(),
                    )
                } else {
                    privateTrustManager
                }
                val context = SSLContext.getInstance("TLS")
                context.init(null, arrayOf(effectiveTrustManager), null)
                val allowed = "https://$publicIp:$authorizedPort"
                return RelayTlsTrust(context.socketFactory, effectiveTrustManager, allowed, authorizedPort)
            } catch (error: GatewayTransportException) {
                throw error
            } catch (_: Exception) {
                throw GatewayTransportException("Relay trust bundle is invalid")
            }
        }

        private fun platformOnly(): RelayTlsTrust {
            val manager = platformTrustManager()
            val context = SSLContext.getInstance("TLS")
            context.init(null, arrayOf(manager), null)
            return RelayTlsTrust(context.socketFactory, manager, null, null)
        }

        private fun requirePublicIpv4(value: String): String {
            val octets = value.split('.')
            require(octets.size == 4)
            val parsed = octets.map { octet ->
                require(octet.isNotEmpty() && octet.all(Char::isDigit))
                require(octet == "0" || !octet.startsWith('0'))
                octet.toInt().also { require(it in 0..255) }
            }
            val first = parsed[0]
            val second = parsed[1]
            require(first in 1..223 && first != 10 && first != 127)
            require(!(first == 169 && second == 254))
            require(!(first == 172 && second in 16..31))
            require(!(first == 192 && second == 168))
            require(!(first == 100 && second in 64..127))
            return parsed.joinToString(".")
        }

        private fun decodeAuthority(raw: JsonObject): Pair<String, X509Certificate> {
            require(raw.keys == AUTHORITY_FIELDS)
            val authorityId = raw.requiredString("authorityId").also {
                require(AUTHORITY_ID.matches(it))
            }
            val encoded = raw.requiredString("certificateDerBase64")
            val der = Base64.getDecoder().decode(encoded)
            require(Base64.getEncoder().encodeToString(der) == encoded)
            require(der.size in 256..MAX_CERTIFICATE_BYTES)
            val certificate = CertificateFactory.getInstance("X.509")
                .generateCertificate(ByteArrayInputStream(der)) as X509Certificate
            certificate.checkValidity()
            require(certificate.basicConstraints >= 0)
            certificate.keyUsage?.let { usage -> require(usage.size > 5 && usage[5]) }
            val certificateDigest = sha256Hex(der)
            val spkiDigest = sha256(certificate.publicKey.encoded)
            require(raw.requiredString("certificateSha256") == certificateDigest)
            require(raw.requiredString("spkiSha256") == spkiDigest.toHex())
            require(
                raw.requiredString("spkiPin") ==
                    "sha256/${Base64.getEncoder().encodeToString(spkiDigest)}",
            )
            return authorityId to certificate
        }

        private fun trustManagerFor(
            certificates: List<Pair<String, X509Certificate>>,
        ): X509TrustManager {
            val keyStore = KeyStore.getInstance(KeyStore.getDefaultType()).apply { load(null) }
            certificates.forEach { (authorityId, certificate) ->
                keyStore.setCertificateEntry("relay-$authorityId", certificate)
            }
            return trustManager(keyStore)
        }

        private fun platformTrustManager(): X509TrustManager = trustManager(null)

        private fun trustManager(keyStore: KeyStore?): X509TrustManager {
            val factory = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm())
            factory.init(keyStore)
            return factory.trustManagers.filterIsInstance<X509TrustManager>().single()
        }

        private fun JsonObject.requiredString(key: String): String =
            getValue(key).jsonPrimitive.contentOrNull?.takeIf(String::isNotBlank)
                ?: error("Missing Relay trust field")

        private fun sha256(value: ByteArray): ByteArray =
            MessageDigest.getInstance("SHA-256").digest(value)

        private fun sha256Hex(value: ByteArray): String = sha256(value).toHex()

        private fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it) }

        private val ROOT_FIELDS = setOf("version", "publicIp", "servicePorts", "authorities")
        private val AUTHORITY_FIELDS = setOf(
            "authorityId",
            "certificateDerBase64",
            "certificateSha256",
            "spkiSha256",
            "spkiPin",
        )
        private const val MAX_BUNDLE_BYTES = 128 * 1024
        private const val MAX_CERTIFICATE_BYTES = 16 * 1024
    }
}

private class FallbackTrustManager(
    private val primary: X509TrustManager,
    private val fallback: X509TrustManager,
) : X509TrustManager {
    override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) =
        primary.checkClientTrusted(chain, authType)

    override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
        try {
            primary.checkServerTrusted(chain, authType)
        } catch (primaryFailure: Exception) {
            try {
                fallback.checkServerTrusted(chain, authType)
            } catch (fallbackFailure: Exception) {
                primaryFailure.addSuppressed(fallbackFailure)
                throw primaryFailure
            }
        }
    }

    override fun getAcceptedIssuers(): Array<X509Certificate> =
        (primary.acceptedIssuers.asSequence() + fallback.acceptedIssuers.asSequence())
            .distinctBy { it.encoded.contentHashCode() }
            .toList()
            .toTypedArray()
}
