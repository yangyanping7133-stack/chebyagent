package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.model.ThreadKey
import java.nio.file.Files
import java.util.UUID
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalTurnImageStoreTest {
    @Test
    fun `staged image survives store recreation and remains bound to one message`() {
        val sandbox = Files.createTempDirectory("local-turn-images").toFile()
        try {
            val root = sandbox.resolve("private-images")
            val thread = ThreadKey("thread-private-value")
            val message = "client-message-001"
            val first = store(root)
            val uploaded = first.put(thread, message, UUID.randomUUID().toString(), ONE_PIXEL_PNG)

            assertTrue(uploaded.assetRef.matches(Regex("ast_[a-f0-9]{64}")))
            assertEquals("image/png", uploaded.mediaType)
            assertTrue(uploaded.androidFile.isFile)
            assertFalse(uploaded.codexPath.contains(thread.value))
            assertFalse(uploaded.codexPath.contains(message))

            val recreated = store(root)
            val resolved = recreated.resolve(thread, message, uploaded.assetRef)
            assertEquals(uploaded.codexPath, resolved.codexPath)
            assertTrue(resolved.androidFile.readBytes().contentEquals(ONE_PIXEL_PNG))

            val wrongBinding = runCatching {
                recreated.resolve(thread, "client-message-002", uploaded.assetRef)
            }.exceptionOrNull()
            assertTrue(wrongBinding is GatewayTransportException)
        } finally {
            sandbox.deleteRecursively()
        }
    }

    @Test
    fun `turn completion deletes claimed image and empty binding directories`() {
        val sandbox = Files.createTempDirectory("local-turn-images").toFile()
        try {
            val root = sandbox.resolve("private-images")
            val imageStore = store(root)
            val uploaded = imageStore.put(
                ThreadKey("thread-a"),
                "client-message-001",
                UUID.randomUUID().toString(),
                ONE_PIXEL_PNG,
            )

            imageStore.claim("turn-001", listOf(uploaded))
            imageStore.release("turn-001")

            assertFalse(uploaded.androidFile.exists())
            assertTrue(root.walkTopDown().none { it.isFile })
        } finally {
            sandbox.deleteRecursively()
        }
    }

    @Test
    fun `invalid image bytes never create a staged file`() {
        val sandbox = Files.createTempDirectory("local-turn-images").toFile()
        try {
            val root = sandbox.resolve("private-images")
            val failure = runCatching {
                store(root).put(
                    ThreadKey("thread-a"),
                    "client-message-001",
                    UUID.randomUUID().toString(),
                    byteArrayOf(1, 2, 3, 4),
                )
            }.exceptionOrNull()

            assertTrue(failure is GatewayTransportException)
            assertFalse(root.exists())
        } finally {
            sandbox.deleteRecursively()
        }
    }

    private fun store(root: java.io.File) = LocalTurnImageStore(
        rootDirectory = root,
        codexRootPath = "/root/.cheby/turn-inputs",
        runtimeReady = { true },
    )

    private companion object {
        val ONE_PIXEL_PNG = java.util.Base64.getDecoder().decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
        )
    }
}
