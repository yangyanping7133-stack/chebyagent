package com.cheby.codex.mobile.data

import android.content.Context
import com.cheby.codex.mobile.model.ThreadKey
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.long
import kotlinx.serialization.json.put

enum class OutboxState {
    QUEUED,
    RETRYABLE,
    /** Gateway accepted this exact client message and returned [OutboxEntry.acceptedTurnId]. */
    ACCEPTED,
    /** This entry is the FIFO head but must wait for [OutboxEntry.waitingForTurnId]. */
    WAITING_TURN,
    NEEDS_SYNC,
    REJECTED,
    NEEDS_ATTACHMENTS,
}

data class OutboxEntry(
    val clientMessageId: String,
    val sessionScope: String,
    val threadKey: ThreadKey,
    val text: String,
    val state: OutboxState,
    val createdAtMillis: Long,
    val retryNotBeforeMillis: Long = 0,
    val sessionGeneration: Long = 0,
    val imageAssetRefs: List<String> = emptyList(),
    val pendingImageCount: Int = 0,
    val acceptedTurnId: String? = null,
    val waitingForTurnId: String? = null,
)

data class OutboxKey(
    val sessionScope: String,
    val sessionGeneration: Long,
    val threadKey: ThreadKey,
    val clientMessageId: String,
)

val OutboxEntry.key: OutboxKey
    get() = OutboxKey(
        sessionScope = sessionScope,
        sessionGeneration = sessionGeneration,
        threadKey = threadKey,
        clientMessageId = clientMessageId,
    )

interface DurableOutboxStore {
    fun list(): List<OutboxEntry>
    fun enqueue(entry: OutboxEntry): Boolean
    fun replacePrepared(
        replacement: OutboxEntry,
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
        activeSessionScope: String,
        activeSessionGeneration: Long,
    ): Boolean
    fun mark(
        key: OutboxKey,
        activeSessionScope: String,
        activeSessionGeneration: Long,
        state: OutboxState,
        retryNotBeforeMillis: Long = 0,
        acceptedTurnId: String? = null,
        waitingForTurnId: String? = null,
    ): Boolean
    fun remove(
        key: OutboxKey,
        activeSessionScope: String,
        activeSessionGeneration: Long,
    ): Boolean
    fun clearScope(expectedSessionScope: String): Boolean
    fun clearOtherScopes(activeSessionScope: String): Boolean
    fun adoptScopeGeneration(
        activeSessionScope: String,
        expectedSessionGeneration: Long,
        activeSessionGeneration: Long,
    ): Boolean
    fun clearOtherGenerations(activeSessionScope: String, activeSessionGeneration: Long): Boolean
    fun clear(): Boolean
}

class SharedPreferencesDurableOutboxStore(context: Context) : DurableOutboxStore {
    private val preferences = context.getSharedPreferences(PREFERENCES_NAME, Context.MODE_PRIVATE)

    override fun list(): List<OutboxEntry> = synchronized(PROCESS_LOCK) {
        decode(preferences.getString(KEY_ENTRIES, null))
    }

    override fun enqueue(entry: OutboxEntry): Boolean = synchronized(PROCESS_LOCK) {
        if (!entry.isValid()) return@synchronized false
        val entries = decode(preferences.getString(KEY_ENTRIES, null))
        val matching = entries.filter { it.key == entry.key }
        if (matching.isNotEmpty()) return@synchronized matching.size == 1 && matching.single() == entry
        persist(entries + entry)
    }

    override fun replacePrepared(
        replacement: OutboxEntry,
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
        activeSessionScope: String,
        activeSessionGeneration: Long,
    ): Boolean = synchronized(PROCESS_LOCK) {
        if (
            activeSessionScope != expectedSessionScope ||
            activeSessionGeneration != expectedSessionGeneration ||
            replacement.sessionScope != expectedSessionScope ||
            replacement.sessionGeneration != expectedSessionGeneration ||
            !replacement.isValid()
        ) return@synchronized false
        val entries = decode(preferences.getString(KEY_ENTRIES, null))
        val matching = entries.indices.filter {
            entries[it].key == replacement.key &&
                entries[it].state == OutboxState.NEEDS_ATTACHMENTS
        }
        if (matching.size != 1) return@synchronized false
        val index = matching.single()
        persist(entries.toMutableList().apply { this[index] = replacement })
    }

    override fun mark(
        key: OutboxKey,
        activeSessionScope: String,
        activeSessionGeneration: Long,
        state: OutboxState,
        retryNotBeforeMillis: Long,
        acceptedTurnId: String?,
        waitingForTurnId: String?,
    ): Boolean = synchronized(PROCESS_LOCK) {
        if (
            activeSessionScope != key.sessionScope ||
            activeSessionGeneration != key.sessionGeneration
        ) return@synchronized false
        val entries = decode(preferences.getString(KEY_ENTRIES, null))
        val matching = entries.indices.filter { entries[it].key == key }
        if (matching.size != 1) return@synchronized false
        val index = matching.single()
        val replacement = entries[index].copy(
            state = state,
            retryNotBeforeMillis = retryNotBeforeMillis,
            acceptedTurnId = acceptedTurnId,
            waitingForTurnId = waitingForTurnId,
        )
        if (!replacement.isValid()) return@synchronized false
        persist(entries.toMutableList().apply { this[index] = replacement })
    }

    override fun remove(
        key: OutboxKey,
        activeSessionScope: String,
        activeSessionGeneration: Long,
    ): Boolean = synchronized(PROCESS_LOCK) {
        if (
            activeSessionScope != key.sessionScope ||
            activeSessionGeneration != key.sessionGeneration
        ) return@synchronized false
        val entries = decode(preferences.getString(KEY_ENTRIES, null))
        val matching = entries.indices.filter { entries[it].key == key }
        if (matching.size != 1) return@synchronized false
        val index = matching.single()
        persist(entries.filterIndexed { entryIndex, _ -> entryIndex != index })
    }

    override fun clearScope(expectedSessionScope: String): Boolean = synchronized(PROCESS_LOCK) {
        val entries = decode(preferences.getString(KEY_ENTRIES, null))
        persist(entries.filterNot { it.sessionScope == expectedSessionScope })
    }

    override fun clearOtherScopes(activeSessionScope: String): Boolean = synchronized(PROCESS_LOCK) {
        val entries = decode(preferences.getString(KEY_ENTRIES, null))
        persist(entries.filter { it.sessionScope == activeSessionScope })
    }

    override fun adoptScopeGeneration(
        activeSessionScope: String,
        expectedSessionGeneration: Long,
        activeSessionGeneration: Long,
    ): Boolean = synchronized(PROCESS_LOCK) {
        if (expectedSessionGeneration < 0 || activeSessionGeneration < 0) {
            return@synchronized false
        }
        val entries = decode(preferences.getString(KEY_ENTRIES, null))
        val adopted = entries.map { entry ->
            if (
                entry.sessionScope == activeSessionScope &&
                entry.sessionGeneration == expectedSessionGeneration
            ) {
                entry.copy(sessionGeneration = activeSessionGeneration)
            } else {
                entry
            }
        }
        if (!adopted.hasUniqueKeys()) return@synchronized false
        persist(adopted)
    }

    override fun clearOtherGenerations(
        activeSessionScope: String,
        activeSessionGeneration: Long,
    ): Boolean = synchronized(PROCESS_LOCK) {
        val entries = decode(preferences.getString(KEY_ENTRIES, null))
        persist(entries.filterNot {
            it.sessionScope == activeSessionScope && it.sessionGeneration != activeSessionGeneration
        })
    }

    override fun clear(): Boolean = synchronized(PROCESS_LOCK) {
        preferences.edit().remove(KEY_ENTRIES).commit()
    }

    private fun persist(entries: List<OutboxEntry>): Boolean {
        val raw = buildJsonArray {
            entries.forEach { entry ->
                add(buildJsonObject {
                    put("clientMessageId", entry.clientMessageId)
                    put("sessionScope", entry.sessionScope)
                    put("threadId", entry.threadKey.value)
                    put("text", entry.text)
                    put("state", entry.state.name)
                    put("createdAtMillis", entry.createdAtMillis)
                    put("retryNotBeforeMillis", entry.retryNotBeforeMillis)
                    put("sessionGeneration", entry.sessionGeneration)
                    put("imageAssetRefs", buildJsonArray {
                        entry.imageAssetRefs.forEach { add(JsonPrimitive(it)) }
                    })
                    put("pendingImageCount", entry.pendingImageCount)
                    entry.acceptedTurnId?.let { put("acceptedTurnId", it) }
                    entry.waitingForTurnId?.let { put("waitingForTurnId", it) }
                })
            }
        }.toString()
        return preferences.edit().putString(KEY_ENTRIES, raw).commit()
    }

    private fun decode(raw: String?): List<OutboxEntry> = if (raw == null) {
        emptyList()
    } else {
        runCatching {
            Json.parseToJsonElement(raw).jsonArray.mapNotNull { element ->
                runCatching {
                    val item = element.jsonObject
                    OutboxEntry(
                        clientMessageId = item.getValue("clientMessageId").jsonPrimitive.content,
                        sessionScope = item.getValue("sessionScope").jsonPrimitive.content,
                        threadKey = ThreadKey(item.getValue("threadId").jsonPrimitive.content),
                        text = item.getValue("text").jsonPrimitive.content,
                        state = OutboxState.valueOf(item.getValue("state").jsonPrimitive.content),
                        createdAtMillis = item.getValue("createdAtMillis").jsonPrimitive.long,
                        retryNotBeforeMillis = item["retryNotBeforeMillis"]?.jsonPrimitive?.long ?: 0,
                        sessionGeneration = item["sessionGeneration"]?.jsonPrimitive?.long ?: 0,
                        imageAssetRefs = item["imageAssetRefs"]?.jsonArray?.map { value ->
                            value.jsonPrimitive.contentOrNull ?: error("Invalid asset reference")
                        }.orEmpty(),
                        pendingImageCount = item["pendingImageCount"]?.jsonPrimitive?.intOrNull ?: 0,
                        acceptedTurnId = item["acceptedTurnId"]?.jsonPrimitive?.contentOrNull,
                        waitingForTurnId = item["waitingForTurnId"]?.jsonPrimitive?.contentOrNull,
                    ).also { require(it.isValid()) }
                }.getOrNull()
            }
        }.getOrDefault(emptyList())
    }

    private companion object {
        val PROCESS_LOCK = Any()
        const val PREFERENCES_NAME = "cheby_outbox"
        const val KEY_ENTRIES = "entries_v1"
    }
}

class InMemoryDurableOutboxStore(
    initial: List<OutboxEntry> = emptyList(),
    private val allowWrites: Boolean = true,
) : DurableOutboxStore {
    private val entries = initial.toMutableList()

    @Synchronized
    override fun list(): List<OutboxEntry> = entries.toList()

    @Synchronized
    override fun enqueue(entry: OutboxEntry): Boolean {
        if (!allowWrites) return false
        if (!entry.isValid()) return false
        val matching = entries.filter { it.key == entry.key }
        if (matching.isNotEmpty()) return matching.size == 1 && matching.single() == entry
        entries += entry
        return true
    }

    @Synchronized
    override fun replacePrepared(
        replacement: OutboxEntry,
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
        activeSessionScope: String,
        activeSessionGeneration: Long,
    ): Boolean {
        if (
            !allowWrites ||
            activeSessionScope != expectedSessionScope ||
            activeSessionGeneration != expectedSessionGeneration ||
            replacement.sessionScope != expectedSessionScope ||
            replacement.sessionGeneration != expectedSessionGeneration ||
            !replacement.isValid()
        ) return false
        val matching = entries.indices.filter {
            entries[it].key == replacement.key &&
                entries[it].state == OutboxState.NEEDS_ATTACHMENTS
        }
        if (matching.size != 1) return false
        val index = matching.single()
        entries[index] = replacement
        return true
    }

    @Synchronized
    override fun mark(
        key: OutboxKey,
        activeSessionScope: String,
        activeSessionGeneration: Long,
        state: OutboxState,
        retryNotBeforeMillis: Long,
        acceptedTurnId: String?,
        waitingForTurnId: String?,
    ): Boolean {
        if (!allowWrites) return false
        if (
            activeSessionScope != key.sessionScope ||
            activeSessionGeneration != key.sessionGeneration
        ) return false
        val matching = entries.indices.filter { entries[it].key == key }
        if (matching.size != 1) return false
        val index = matching.single()
        val replacement = entries[index].copy(
            state = state,
            retryNotBeforeMillis = retryNotBeforeMillis,
            acceptedTurnId = acceptedTurnId,
            waitingForTurnId = waitingForTurnId,
        )
        if (!replacement.isValid()) return false
        entries[index] = replacement
        return true
    }

    @Synchronized
    override fun remove(
        key: OutboxKey,
        activeSessionScope: String,
        activeSessionGeneration: Long,
    ): Boolean {
        if (!allowWrites) return false
        if (
            activeSessionScope != key.sessionScope ||
            activeSessionGeneration != key.sessionGeneration
        ) return false
        val matching = entries.indices.filter { entries[it].key == key }
        if (matching.size != 1) return false
        entries.removeAt(matching.single())
        return true
    }

    @Synchronized
    override fun clearScope(expectedSessionScope: String): Boolean {
        if (!allowWrites) return false
        entries.removeAll { it.sessionScope == expectedSessionScope }
        return true
    }

    @Synchronized
    override fun clearOtherScopes(activeSessionScope: String): Boolean {
        if (!allowWrites) return false
        entries.removeAll { it.sessionScope != activeSessionScope }
        return true
    }

    @Synchronized
    override fun adoptScopeGeneration(
        activeSessionScope: String,
        expectedSessionGeneration: Long,
        activeSessionGeneration: Long,
    ): Boolean {
        if (
            !allowWrites ||
            expectedSessionGeneration < 0 ||
            activeSessionGeneration < 0
        ) return false
        val adopted = entries.map { entry ->
            if (
                entry.sessionScope == activeSessionScope &&
                entry.sessionGeneration == expectedSessionGeneration
            ) {
                entry.copy(sessionGeneration = activeSessionGeneration)
            } else {
                entry
            }
        }
        if (!adopted.hasUniqueKeys()) return false
        entries.clear()
        entries += adopted
        return true
    }

    @Synchronized
    override fun clearOtherGenerations(
        activeSessionScope: String,
        activeSessionGeneration: Long,
    ): Boolean {
        if (!allowWrites) return false
        entries.removeAll {
            it.sessionScope == activeSessionScope && it.sessionGeneration != activeSessionGeneration
        }
        return true
    }

    @Synchronized
    override fun clear(): Boolean {
        if (!allowWrites) return false
        entries.clear()
        return true
    }
}

private val ASSET_REF = Regex("[A-Za-z0-9_-]{32,128}")
private val TURN_ID = Regex("[^\\p{Cc}\\p{Cf}\\s]{1,256}")

private fun List<OutboxEntry>.hasUniqueKeys(): Boolean =
    map(OutboxEntry::key).distinct().size == size

private fun OutboxEntry.isValid(): Boolean =
    clientMessageId.length in 8..128 &&
        sessionScope.isNotBlank() &&
        sessionGeneration >= 0 &&
        imageAssetRefs.size <= 10 &&
        imageAssetRefs.all(ASSET_REF::matches) &&
        pendingImageCount in 0..10 &&
        !(pendingImageCount > 0 && imageAssetRefs.isNotEmpty()) &&
        when (state) {
            OutboxState.ACCEPTED ->
                acceptedTurnId?.matches(TURN_ID) == true && waitingForTurnId == null
            OutboxState.NEEDS_SYNC ->
                (acceptedTurnId == null || acceptedTurnId.matches(TURN_ID)) &&
                    waitingForTurnId == null
            OutboxState.WAITING_TURN ->
                acceptedTurnId == null &&
                    (waitingForTurnId == null || waitingForTurnId.matches(TURN_ID))
            else -> acceptedTurnId == null && waitingForTurnId == null
        } &&
        if (state == OutboxState.NEEDS_ATTACHMENTS) {
            pendingImageCount > 0 && imageAssetRefs.isEmpty()
        } else {
            pendingImageCount == 0
        }
