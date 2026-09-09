package com.chebysight.chebyagent.android;

import org.junit.Test;
import org.json.JSONObject;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public final class PolicyGateTest {
    @Test
    public void realTapDecisionAllowsUnknownAndFormerlyProtectedTargets() throws Exception {
        for (String label : new String[] {"", "Website", "Позвонить", "Send SMS", "credit card"}) {
            JSONObject target = new JSONObject().put("text", label).put("found", false)
                    .put("active_package", "com.android.bank").put("screen_text", "Оплатить")
                    .put("screen_has_semantics", false);
            JSONObject decision = PolicyGate.evaluateTapEvidence(target, false);
            assertTrue(label, decision.getBoolean("allowed"));
            assertFalse(decision.getBoolean("confirmation_required"));
        }
    }

    @Test
    public void realCheckoutDecisionHonorsOwnerConsent() throws Exception {
        JSONObject target = new JSONObject().put("text", "Оплатить");
        JSONObject pending = PolicyGate.evaluateTapEvidence(target, false);
        assertFalse(pending.getBoolean("allowed"));
        assertTrue(pending.getBoolean("confirmation_required"));
        assertEquals("awaiting_owner_confirmation", pending.getString("status"));
        JSONObject approved = PolicyGate.evaluateTapEvidence(target, true);
        assertTrue(approved.getBoolean("allowed"));
        assertEquals("local_owner", approved.getString("authorization_source"));
    }

    @Test
    public void actualLaunchPolicyHasNoPackageCategoryBlock() throws Exception {
        for (String name : new String[] {"com.android.bank", "com.huawei.browser", "com.android.dialer", "com.tencent.mm"}) {
            assertTrue(name, PolicyGate.evaluateOpenApp(name).getBoolean("allowed"));
        }
        assertTrue(PolicyGate.evaluateOpenUrl(null, null).getBoolean("allowed"));
    }

    @Test
    public void agentCannotOperateOwnerAuthorizationUiEvenWithStandingConsent() throws Exception {
        CommerceAuthorization.ownerInteractionActive = true;
        try {
            assertFalse(PolicyGate.evaluateOpenApp("ru.cian.main").getBoolean("allowed"));
            assertFalse(PolicyGate.evaluateOpenUrl(null, null).getBoolean("allowed"));
            assertFalse(PolicyGate.evaluateTapEvidence(new JSONObject().put("text", "Оплатить"), true).getBoolean("allowed"));
            assertFalse(PolicyGate.evaluateSwipe(null).getBoolean("allowed"));
            assertFalse(PolicyGate.evaluateInputText(null).getBoolean("allowed"));
        } finally {
            CommerceAuthorization.ownerInteractionActive = false;
        }
    }

    @Test
    public void exactOwnChatComposerIsNotAPaymentForm() {
        assertTrue(PolicyGate.isOwnMessageComposer("com.termux", "com.termux", "com.termux",
                "message_composer", "android.widget.EditText", true, true));
        assertTrue(PolicyGate.isOwnMessageComposer("com.cheby.codex.mobile", "com.cheby.codex.mobile",
                "com.cheby.codex.mobile", "message_composer", "android.widget.EditText", true, true));
        assertTrue(PolicyGate.containsFinancialRiskTerm("Yandex Go 需要先选择有效支付方式"));
    }

    @Test
    public void externalInputsCannotImpersonateOwnComposer() {
        assertFalse(PolicyGate.isOwnMessageComposer("com.termux", "ru.yandex.taxi", "ru.yandex.taxi",
                "message_composer", "android.widget.EditText", true, true));
        assertFalse(PolicyGate.isOwnMessageComposer("com.termux", "com.termux", "com.example.bank",
                "message_composer", "android.widget.EditText", true, true));
        assertFalse(PolicyGate.isOwnMessageComposer("com.example.bank", "com.example.bank", "com.example.bank",
                "message_composer", "android.widget.EditText", true, true));
    }

    @Test
    public void otherOwnInputsAndUnknownContextRemainProtected() {
        assertFalse(PolicyGate.isOwnMessageComposer("com.termux", "com.termux", "com.termux",
                "payment_input", "android.widget.EditText", true, true));
        assertFalse(PolicyGate.isOwnMessageComposer("com.termux", "com.termux", "com.termux",
                "message_composer", "android.webkit.WebView", true, true));
        assertFalse(PolicyGate.isOwnMessageComposer("com.termux", "com.termux", "com.termux",
                "message_composer", "android.widget.EditText", false, true));
        assertFalse(PolicyGate.isOwnMessageComposer("com.termux", "com.termux", "com.termux",
                "message_composer", "android.widget.EditText", true, false));
    }

    @Test
    public void mapAddressesAndShopNamesDoNotBecomeBankEvidence() {
        // Real phone input_text and swipe_screen refusals, 2026-09-07.
        assertFalse(PolicyGate.containsFinancialRiskTerm(
                "Massage salon\nru.yandex.yandexmaps:id/search_line_edit_text\n"
                        + "Massage salon · Fontanka River Embankment, 102"));
        assertFalse(PolicyGate.containsFinancialRiskTerm("Pet salon\nBubochka"));
        assertFalse(PolicyGate.containsFinancialRiskTerm("BUBOCHKA\nEMBANKMENT"));
    }

    @Test
    public void longTermSearchIsNotDebtButActualDebtRemainsRecognized() {
        for (String label : new String[] {"Снять надолго", "Долгосрочная аренда", "Долгосрочный прогноз"}) {
            assertFalse(label, PolicyGate.containsFinancialRiskTerm(label));
        }
        for (String label : new String[] {"Погасить долг", "Сумма долга", "Оплата долгов", "ДОЛГ", "долг_button"}) {
            assertTrue(label, PolicyGate.containsFinancialRiskTerm(label));
        }
    }

    @Test
    public void financialTokensActionsAndRealPackagesStayProtected() {
        for (String evidence : new String[] {
                "Bank", "BOC", "bank_account", "com.android.bank", "Bubochka\nbank",
                "Embankment\nоплатить", "Pay", "付款", "credit card", "payment_button"
        }) {
            assertTrue(evidence, PolicyGate.containsFinancialRiskTerm(evidence));
        }
        for (String packageName : new String[] {
                "com.cmbchina", "com.icbc.mobile", "com.example.walletapp", "com.eg.android.alipaygphone"
        }) {
            assertTrue(packageName, PolicyGate.isProtectedMutationPackage(packageName));
        }
    }

    @Test
    public void browserAndUrlLaunchingAreAvailable() {
        assertTrue(PolicyGate.isBrowserUrlLaunchingEnabled());
    }

    @Test
    public void ordinaryTargetsDoNotRequireCheckoutConsent() {
        for (String evidence : new String[] {"", "Website", "Перейти на сайт", "Позвонить",
                "Send SMS", "com.android.bank", "credit card", "Payment methods", "打开 Pay",
                "Снять надолго", "com.huawei.browser", "com.tencent.mm"}) {
            assertFalse(evidence, PolicyGate.requiresCheckoutConsent(evidence));
        }
        assertFalse(PolicyGate.requiresCheckoutConsent(null));
    }

    @Test
    public void checkoutCommitsStillRequireOwnerConsent() {
        for (String evidence : new String[] {"Pay now", "确认付款", "Оплатить",
                "Оформить заказ", "确认订单", "Confirm ride"}) {
            assertTrue(evidence, PolicyGate.requiresCheckoutConsent(evidence));
        }
    }

    @Test
    public void browserPackagesCannotBeOpenedDirectly() {
        assertTrue(PolicyGate.isBrowserPackage("com.huawei.browser"));
        assertTrue(PolicyGate.isBrowserPackage("com.android.chrome"));
        assertFalse(PolicyGate.isBrowserPackage("ru.yandex.yandexmaps"));
    }

    @Test
    public void unrelatedPayLabelElsewhereOnScreenDoesNotBlockDeliveryTarget() {
        assertFalse(PolicyGate.hasTapFinancialRisk(
                "外卖.\nru.yandex.taxi\nandroid.view.View",
                "外卖\n美食\n打开 Pay"));
    }

    @Test
    public void payTargetItselfRemainsBlocked() {
        assertTrue(PolicyGate.hasTapFinancialRisk(
                "打开 Pay\nru.yandex.taxi\nandroid.view.View",
                "外卖\n美食\n打开 Pay"));
    }

    @Test
    public void finalOrderAndBookingTargetsRemainBlocked() {
        assertTrue(PolicyGate.hasTapConsequentialCommitRisk("Оформить заказ"));
        assertTrue(PolicyGate.hasTapConsequentialCommitRisk("确认预约"));
        assertTrue(PolicyGate.hasTapConsequentialCommitRisk("Book online"));
        assertTrue(PolicyGate.hasTapConsequentialCommitRisk("Записаться онлайн"));
        assertFalse(PolicyGate.hasTapConsequentialCommitRisk("外卖."));
    }

    @Test
    public void websiteTargetsRemainBlocked() {
        assertTrue(PolicyGate.hasTapBrowserLaunchRisk("Go to website"));
        assertTrue(PolicyGate.hasTapBrowserLaunchRisk("Перейти на сайт"));
        assertFalse(PolicyGate.hasTapBrowserLaunchRisk("Goods and services"));
    }

    @Test
    public void screenWithoutAccessibilitySemanticsFailsClosed() {
        assertFalse(PolicyGate.hasSafeScreenContext(true, "com.example.canvas", false));
    }

    @Test
    public void identifiedSemanticScreenPassesContextGate() {
        assertTrue(PolicyGate.hasSafeScreenContext(true, "com.example.maps", true));
    }

    @Test
    public void missingPackageOrRootFailsClosed() {
        assertFalse(PolicyGate.hasSafeScreenContext(false, "com.example.maps", true));
        assertFalse(PolicyGate.hasSafeScreenContext(true, "", true));
    }

    @Test
    public void liveRootPackageWinsOverBlankOrStaleEventPackage() {
        assertEquals(
                "com.huawei.android.launcher",
                PolicyGate.authoritativeScreenPackage("com.huawei.android.launcher", "")
        );
        assertEquals(
                "com.huawei.android.launcher",
                PolicyGate.authoritativeScreenPackage(
                        "com.huawei.android.launcher",
                        "com.android.systemui"
                )
        );
        assertEquals(
                "com.android.systemui",
                PolicyGate.authoritativeScreenPackage("", "com.android.systemui")
        );
    }

    @Test
    public void toolCallStatusIsNotMistakenForPhoneCallAuthorization() {
        assertFalse(PolicyGate.containsCallSmsAuthorizationTerm("Tool call completed.\nandroid_phone_status"));
        assertFalse(PolicyGate.containsCallSmsAuthorizationTerm(
                "CHEBY_V19_HI_OK; phone_online=true; foreground=com.cheby.codex.mobile"));
    }

    @Test
    public void explicitCallAndSmsActionsRemainProtected() {
        assertTrue(PolicyGate.containsCallSmsAuthorizationTerm("Call"));
        assertTrue(PolicyGate.containsCallSmsAuthorizationTerm("Call Alice"));
        assertTrue(PolicyGate.containsCallSmsAuthorizationTerm("发送短信"));
        assertFalse(PolicyGate.containsCallSmsAuthorizationTerm("发送消息"));
    }

    @Test
    public void chatHistorySignalCannotBlockSafeComposerTap() {
        assertFalse(PolicyGate.hasCallSmsTapRisk(
                "消息输入框\nmessage_composer\ncom.cheby.codex.mobile",
                "com.cheby.codex.mobile"));
    }

    @Test
    public void dialerPackageAndCallButtonRemainProtected() {
        assertTrue(PolicyGate.hasCallSmsTapRisk("Dial pad", "com.android.dialer"));
        assertTrue(PolicyGate.hasCallSmsTapRisk("Call Alice", "com.example.safe"));
    }

    @Test
    public void emptyComposeDecorationCannotDisplaceSemanticComposer() {
        assertFalse(PolicyGate.preferTapTarget(
                true, true, 3, 48_384,
                false, 4, 11_520));
    }

    @Test
    public void semanticChildCanDisplaceEmptyContainer() {
        assertTrue(PolicyGate.preferTapTarget(
                true, false, 3, 48_384,
                true, 4, 11_520));
    }

    @Test
    public void deeperSemanticTargetWinsAmongSemanticNodes() {
        assertTrue(PolicyGate.preferTapTarget(
                true, true, 3, 48_384,
                true, 4, 11_520));
    }

    @Test
    public void exactOwnMediaProjectionConsentBypassesWarningTextFalsePositive() {
        String exactScreen = "是否允许“ChebyNode”录制/投射您的屏幕\n禁止\nandroid:id/button2\n允许\nandroid:id/button1";
        assertTrue(PolicyGate.isOwnMediaProjectionConsent(
                "com.android.systemui",
                "com.android.systemui",
                true,
                true,
                "android.widget.Button",
                "android:id/button1",
                "允许",
                exactScreen,
                "ChebyNode"
        ));
        String embeddedScreen = "是否允许“ChebyCodex”录制/投射您的屏幕\n禁止\nandroid:id/button2\n允许\nandroid:id/button1";
        assertTrue(PolicyGate.isOwnMediaProjectionConsent(
                "com.android.systemui",
                "com.android.systemui",
                true,
                true,
                "android.widget.Button",
                "android:id/button1",
                "允许",
                embeddedScreen,
                "ChebyCodex"
        ));
        assertFalse(PolicyGate.isOwnMediaProjectionConsent(
                "com.android.systemui",
                "com.android.systemui",
                true,
                true,
                "android.widget.Button",
                "android:id/button1",
                "允许",
                "是否允许“Other App”录制/投射您的屏幕\n禁止\nandroid:id/button2",
                "ChebyNode"
        ));
        assertFalse(PolicyGate.isOwnMediaProjectionConsent(
                "com.android.systemui",
                "com.android.systemui",
                true,
                true,
                "android.widget.Button",
                "android:id/button2",
                "禁止",
                exactScreen,
                "ChebyNode"
        ));
        assertFalse(PolicyGate.isOwnMediaProjectionConsent(
                "com.android.systemui",
                "com.android.systemui",
                true,
                true,
                "android.widget.Button",
                "android:id/button1",
                "允许",
                embeddedScreen,
                "ChebyNode"
        ));
    }
}
