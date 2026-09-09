package com.cheby.codex.mobile.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import org.junit.Rule
import org.junit.Test

class PairingSecurityInstrumentedTest {
    @get:Rule
    val compose = createComposeRule()

    @Test
    fun secretKeyIsExposedAsSensitivePasswordInput() {
        compose.setContent {
            MaterialTheme {
                PairingScreen(
                    isPairing = false,
                    error = null,
                    canCancel = false,
                    addressOnly = false,
                    onCancel = {},
                    onPair = { _, _, _ -> },
                    onChangeServer = {},
                )
            }
        }

        compose.onNodeWithText("SK")
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.Password, Unit))
    }
}
