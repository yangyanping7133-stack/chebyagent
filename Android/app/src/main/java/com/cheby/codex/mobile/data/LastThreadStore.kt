package com.cheby.codex.mobile.data

import android.content.Context
import com.cheby.codex.mobile.model.ThreadKey

interface LastThreadStore {
    fun read(): ThreadKey?
    fun write(threadKey: ThreadKey?)
}

class SharedPreferencesLastThreadStore(context: Context) : LastThreadStore {
    private val preferences = context.getSharedPreferences("cheby_session", Context.MODE_PRIVATE)

    override fun read(): ThreadKey? = preferences.getString(KEY_LAST_THREAD, null)
        ?.takeIf(String::isNotBlank)
        ?.let(::ThreadKey)

    override fun write(threadKey: ThreadKey?) {
        preferences.edit().apply {
            if (threadKey == null) remove(KEY_LAST_THREAD) else putString(KEY_LAST_THREAD, threadKey.value)
        }.apply()
    }

    private companion object {
        const val KEY_LAST_THREAD = "last_thread"
    }
}

class InMemoryLastThreadStore(initial: ThreadKey? = null) : LastThreadStore {
    private var value = initial

    override fun read(): ThreadKey? = value

    override fun write(threadKey: ThreadKey?) {
        value = threadKey
    }
}
