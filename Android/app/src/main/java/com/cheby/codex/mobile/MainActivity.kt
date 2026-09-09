package com.cheby.codex.mobile

import android.content.Context
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.core.view.WindowCompat
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewmodel.compose.viewModel
import com.cheby.codex.mobile.data.SharedPreferencesLastThreadStore
import com.cheby.codex.mobile.data.SharedPreferencesDurableOutboxStore
import com.cheby.codex.mobile.gateway.GatewayFactory
import com.cheby.codex.mobile.ui.AppViewModel
import com.cheby.codex.mobile.ui.AndroidImageAttachmentReader
import com.cheby.codex.mobile.ui.ChebyCodexApp
import com.cheby.codex.mobile.ui.theme.ChebyCodexTheme
import com.cheby.codex.runtime.EmbeddedRuntimeRegistry
import java.util.concurrent.atomic.AtomicLong

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        EmbeddedRuntimeRegistry.prepareIfPresent(this)
        // JUY-AL00 (Android 10) pans edge-to-edge Compose windows even when the
        // manifest requests adjustResize. Let the decor own system insets so the
        // app bar remains fixed while the IME resizes the conversation viewport.
        WindowCompat.setDecorFitsSystemWindows(window, true)
        // This object is intentionally cheap. ViewModelProvider invokes create() only when there
        // is no retained AppViewModel, so rotation cannot construct a competing remote gateway.
        val factory = MainActivityViewModelFactory(applicationContext)
        setContent {
            ChebyCodexTheme {
                val appViewModel: AppViewModel = viewModel(factory = factory)
                ChebyCodexApp(appViewModel)
            }
        }
    }
}

internal class MainActivityViewModelFactory(
    context: Context,
) : ViewModelProvider.Factory {
    private val applicationContext = context.applicationContext

    override fun <T : ViewModel> create(modelClass: Class<T>): T {
        require(modelClass.isAssignableFrom(AppViewModel::class.java))
        dependencySetCreations.incrementAndGet()
        @Suppress("UNCHECKED_CAST")
        return AppViewModel(
            gateway = GatewayFactory.create(applicationContext),
            lastThreadStore = SharedPreferencesLastThreadStore(applicationContext),
            outboxStore = SharedPreferencesDurableOutboxStore(applicationContext),
            imageAttachmentReader = AndroidImageAttachmentReader(
                resolver = applicationContext.contentResolver,
                trustedCameraAuthority = "${applicationContext.packageName}.fileprovider",
            ),
        ) as T
    }

    internal companion object {
        private val dependencySetCreations = AtomicLong(0)

        fun creationCountForTests(): Long = dependencySetCreations.get()
    }
}
