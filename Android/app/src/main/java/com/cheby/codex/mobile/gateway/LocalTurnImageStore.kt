package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.model.ThreadKey
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest
import java.security.SecureRandom
import java.nio.file.Files
import java.util.Comparator
import java.util.concurrent.ConcurrentHashMap

/**
 * Private, crash-recoverable staging for Codex app-server `localImage` turn inputs.
 *
 * Android writes inside the embedded Debian rootfs. Codex receives only the corresponding
 * in-guest path. Raw Thread and message identifiers never appear in a file name.
 */
internal class LocalTurnImageStore(
    private val rootDirectory: File,
    private val codexRootPath: String,
    private val runtimeReady: () -> Boolean,
    private val nowMillis: () -> Long = System::currentTimeMillis,
) {
    private val claimedByTurn = ConcurrentHashMap<String, Set<StoredImage>>()
    private val random = SecureRandom()

    fun isAvailable(): Boolean {
        if (!runtimeReady()) return false
        if (rootDirectory.exists() && (!rootDirectory.isDirectory || isSymbolicLink(rootDirectory))) {
            return false
        }
        return true
    }

    @Synchronized
    fun put(
        threadKey: ThreadKey,
        clientMessageId: String,
        clientAssetId: String,
        bytes: ByteArray,
    ): StoredImage {
        requireBinding(threadKey, clientMessageId, clientAssetId)
        if (!isAvailable() || bytes.isEmpty() || bytes.size > MAX_UPLOAD_BYTES) {
            throw GatewayTransportException("Image input is unavailable")
        }
        val mediaType = detectMediaType(bytes)
        sweepExpired()
        val retained = retainedUsage()
        if (retained.files >= MAX_RETAINED_FILES || retained.bytes + bytes.size > MAX_RETAINED_BYTES) {
            throw GatewayTransportException("Image input is unavailable")
        }
        val bindingDirectory = bindingDirectory(threadKey, clientMessageId)
        ensurePrivateDirectory(bindingDirectory)
        val assetRef = "ast_${randomHex(32)}"
        val extension = if (mediaType == "image/png") "png" else "jpg"
        val privateName = privateFileStem(assetRef)
        val target = File(bindingDirectory, "$privateName.$extension")
        val temporary = File(bindingDirectory, ".$privateName.part")
        if (target.exists() || temporary.exists() || isSymbolicLink(target) || isSymbolicLink(temporary)) {
            throw GatewayTransportException("Image input is unavailable")
        }
        try {
            FileOutputStream(temporary, false).use { output ->
                output.write(bytes)
                output.fd.sync()
            }
            setOwnerOnly(temporary)
            if (!temporary.renameTo(target)) {
                throw GatewayTransportException("Image input is unavailable")
            }
            setOwnerOnly(target)
            return StoredImage(
                assetRef = assetRef,
                mediaType = mediaType,
                byteCount = bytes.size,
                androidFile = target,
                codexPath = codexPath(threadKey, clientMessageId, target.name),
            )
        } catch (error: Exception) {
            temporary.delete()
            target.delete()
            if (error is GatewayTransportException) throw error
            throw GatewayTransportException("Image input is unavailable")
        }
    }

    @Synchronized
    fun resolve(
        threadKey: ThreadKey,
        clientMessageId: String,
        assetRef: String,
    ): StoredImage {
        if (!ASSET_REF.matches(assetRef) || !isAvailable()) {
            throw GatewayTransportException("Image input is invalid")
        }
        val directory = bindingDirectory(threadKey, clientMessageId)
        val privateName = privateFileStem(assetRef)
        val candidates = listOf(File(directory, "$privateName.png"), File(directory, "$privateName.jpg"))
        val file = candidates.singleOrNull { it.isFile && !isSymbolicLink(it) }
            ?: throw GatewayTransportException("Image input is invalid")
        if (!isInsideRoot(file)) throw GatewayTransportException("Image input is invalid")
        val mediaType = if (file.extension == "png") "image/png" else "image/jpeg"
        if (file.length() !in 1..MAX_UPLOAD_BYTES.toLong()) {
            throw GatewayTransportException("Image input is invalid")
        }
        return StoredImage(
            assetRef = assetRef,
            mediaType = mediaType,
            byteCount = file.length().toInt(),
            androidFile = file,
            codexPath = codexPath(threadKey, clientMessageId, file.name),
        )
    }

    fun claim(turnId: String, images: List<StoredImage>) {
        if (turnId.isBlank() || images.isEmpty()) return
        claimedByTurn[turnId] = images.toSet()
    }

    fun release(turnId: String) {
        val claimed = claimedByTurn.remove(turnId).orEmpty()
        claimed.forEach { image ->
            image.androidFile.delete()
            pruneEmptyParents(image.androidFile.parentFile)
        }
    }

    @Synchronized
    fun sweepExpired() {
        if (!rootDirectory.isDirectory || isSymbolicLink(rootDirectory)) return
        val cutoff = nowMillis() - RETENTION_MILLIS
        runCatching {
            Files.walk(rootDirectory.toPath()).use { paths ->
                paths.sorted(Comparator.reverseOrder()).forEach { path ->
                    val file = path.toFile()
                    when {
                        file == rootDirectory -> Unit
                        isSymbolicLink(file) -> file.delete()
                        file.isFile && (file.lastModified() <= cutoff || file.name.endsWith(".part")) ->
                            file.delete()
                        file.isDirectory && file.list().isNullOrEmpty() -> file.delete()
                    }
                }
            }
        }
    }

    private fun bindingDirectory(threadKey: ThreadKey, clientMessageId: String): File =
        File(File(rootDirectory, digestName(threadKey.value)), digestName(clientMessageId))

    private fun codexPath(threadKey: ThreadKey, clientMessageId: String, fileName: String): String =
        "$codexRootPath/${digestName(threadKey.value)}/${digestName(clientMessageId)}/$fileName"

    private fun requireBinding(threadKey: ThreadKey, clientMessageId: String, clientAssetId: String) {
        if (
            threadKey.value.isBlank() ||
            clientMessageId.length !in 8..128 ||
            clientMessageId.any { it.isWhitespace() || it.isISOControl() } ||
            !UUID_TEXT.matches(clientAssetId)
        ) {
            throw GatewayTransportException("Image input is invalid")
        }
    }

    private fun retainedUsage(): RetainedUsage {
        if (!rootDirectory.isDirectory) return RetainedUsage(0, 0)
        return runCatching {
            var files = 0
            var bytes = 0L
            Files.walk(rootDirectory.toPath()).use { paths ->
                paths.forEach { path ->
                    val file = path.toFile()
                    if (file.isFile && !file.name.endsWith(".part") && !isSymbolicLink(file)) {
                        files += 1
                        bytes += file.length()
                    }
                }
            }
            RetainedUsage(files, bytes)
        }.getOrElse {
            RetainedUsage(MAX_RETAINED_FILES, MAX_RETAINED_BYTES)
        }
    }

    private fun ensurePrivateDirectory(directory: File) {
        if (!rootDirectory.exists() && !rootDirectory.mkdirs()) {
            throw GatewayTransportException("Image input is unavailable")
        }
        if (!rootDirectory.isDirectory || isSymbolicLink(rootDirectory) || !isInsideRoot(rootDirectory)) {
            throw GatewayTransportException("Image input is unavailable")
        }
        if (!directory.exists() && !directory.mkdirs()) {
            throw GatewayTransportException("Image input is unavailable")
        }
        var current: File? = directory
        while (current != null && isInsideRoot(current)) {
            if (!current.isDirectory || isSymbolicLink(current)) {
                throw GatewayTransportException("Image input is unavailable")
            }
            setOwnerOnly(current)
            if (current == rootDirectory) break
            current = current.parentFile
        }
    }

    private fun isInsideRoot(file: File): Boolean = runCatching {
        val root = rootDirectory.canonicalFile.path + File.separator
        val candidate = file.canonicalFile.path
        candidate == rootDirectory.canonicalFile.path || candidate.startsWith(root)
    }.getOrDefault(false)

    private fun pruneEmptyParents(start: File?) {
        var current = start
        while (current != null && current != rootDirectory && isInsideRoot(current)) {
            if (!current.isDirectory || !current.list().isNullOrEmpty()) return
            if (!current.delete()) return
            current = current.parentFile
        }
    }

    private fun detectMediaType(bytes: ByteArray): String = when {
        bytes.size >= PNG_SIGNATURE.size + PNG_IEND.size &&
            PNG_SIGNATURE.indices.all { bytes[it] == PNG_SIGNATURE[it] } &&
            PNG_IEND.indices.all { bytes[bytes.size - PNG_IEND.size + it] == PNG_IEND[it] } ->
            "image/png"
        bytes.size >= 4 &&
            bytes[0] == 0xff.toByte() && bytes[1] == 0xd8.toByte() &&
            bytes[bytes.lastIndex - 1] == 0xff.toByte() && bytes[bytes.lastIndex] == 0xd9.toByte() ->
            "image/jpeg"
        else -> throw GatewayTransportException("Image input is invalid")
    }

    private fun digestName(value: String): String = sha256(value.toByteArray(Charsets.UTF_8)).take(32)

    private fun privateFileStem(assetRef: String): String =
        "img_${sha256(assetRef.toByteArray(Charsets.US_ASCII))}"

    private fun randomHex(byteCount: Int): String = ByteArray(byteCount).also(random::nextBytes)
        .joinToString("") { "%02x".format(it.toInt() and 0xff) }

    private fun sha256(value: ByteArray): String = MessageDigest.getInstance("SHA-256")
        .digest(value)
        .joinToString("") { "%02x".format(it.toInt() and 0xff) }

    private fun setOwnerOnly(file: File) {
        if (
            !file.setReadable(false, false) ||
            !file.setWritable(false, false) ||
            !file.setExecutable(false, false) ||
            !file.setReadable(true, true) ||
            !file.setWritable(true, true) ||
            file.isDirectory && !file.setExecutable(true, true)
        ) {
            throw GatewayTransportException("Image input is unavailable")
        }
    }

    private fun isSymbolicLink(file: File): Boolean = runCatching {
        Files.isSymbolicLink(file.toPath())
    }.getOrDefault(true)

    data class StoredImage(
        val assetRef: String,
        val mediaType: String,
        val byteCount: Int,
        val androidFile: File,
        val codexPath: String,
    )

    private data class RetainedUsage(val files: Int, val bytes: Long)

    private companion object {
        const val MAX_UPLOAD_BYTES = 8 * 1024 * 1024
        const val MAX_RETAINED_FILES = 32
        const val MAX_RETAINED_BYTES = 128L * 1024L * 1024L
        const val RETENTION_MILLIS = 24L * 60L * 60L * 1_000L
        val ASSET_REF = Regex("ast_[a-f0-9]{64}")
        val UUID_TEXT = Regex("[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
        val PNG_SIGNATURE = byteArrayOf(
            0x89.toByte(), 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
        )
        val PNG_IEND = byteArrayOf(
            0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e, 0x44,
            0xae.toByte(), 0x42, 0x60, 0x82.toByte(),
        )
    }
}
