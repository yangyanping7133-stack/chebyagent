package com.chebysight.chebyagent.android;

import android.content.Context;
import android.net.Uri;

import org.json.JSONObject;

import java.util.Locale;

final class PolicyGate {
    private static final String[] FINANCIAL_RISK_TERMS = {
            "pay", "payment", "transfer", "loan", "borrow", "credit", "installment",
            "cash advance", "lending", "mortgage", "debt",
            "支付", "付款", "转账", "收款", "钱包", "贷款", "借款", "借钱", "借呗", "花呗", "微粒贷",
            "分付", "白条", "金条", "分期", "信用卡", "额度", "授信", "提额", "现金贷", "网贷",
            "借贷", "还款", "欠款", "年化", "利率",
            "оплатить", "оплата", "платеж", "кредит", "займ", "ипотека", "рассрочка"
    };

    // Debt is a word, not the substring in надолго (long-term) or долгосрочный.
    private static final String[] DEBT_WORDS = {
            "долг", "долга", "долгу", "долгом", "долге", "долги", "долгов", "долгам", "долгами", "долгах"
    };

    private static final String[] FINANCIAL_PACKAGE_TERMS = {
            "bank", "icbc", "cmb", "ccb", "boc", "abchina", "bankcomm", "psbc", "spdb",
            "citic", "cebbank", "hxb", "pingan", "paic", "alipay", "antfortune",
            "mybank", "jdjr", "duxiaoman", "fenqile", "lufax", "loan", "credit", "finance", "wallet"
    };

    private static final String[] CONSEQUENTIAL_COMMIT_TERMS = {
            "place order", "submit order", "confirm order", "complete order",
            "book now", "book online", "online booking", "confirm booking",
            "request ride", "order taxi", "confirm ride",
            "提交订单", "确认订单", "立即下单", "确认预约", "立即预约", "确认叫车",
            "оформить заказ", "подтвердить заказ", "заказать такси",
            "подтвердить бронирование", "забронировать", "онлайн-запись", "записаться онлайн"
    };

    private static final String[] BROWSER_LAUNCH_TERMS = {
            "website", "open website", "go to website", "visit website", "web site",
            "сайт", "открыть сайт", "перейти на сайт"
    };

    private static final String[] BROWSER_PACKAGE_TERMS = {
            "browser", "chrome", "firefox", "yandex.browser", "opera", "edge"
    };

    private static final String[] FINANCIAL_URL_SCHEMES = {"alipay", "alipays", "wxpay", "unionpay", "uppay"};
    private static final String[] PROTECTED_MUTATION_PACKAGES = {
            "com.tencent.mm", "com.tencent.mobileqq", "com.eg.android.alipaygphone",
            "com.unionpay", "com.icbc", "com.cmbchina", "com.chinamworld", "com.android.bank"
    };
    private static final String[] CALL_TERMS = {
            "dial", "phone call", "voice call", "video call", "make a call", "place a call", "start a call",
            "拨号", "拨打", "呼叫", "打电话", "позвонить", "звонить", "вызов"
    };
    private static final String[] SMS_CONTEXT_TERMS = {"sms", "mms", "text message", "短信", "смс"};
    private static final String[] SMS_SEND_TERMS = {"send", "发送", "发", "отправить"};
    private static final String[] CALL_SMS_MUTATION_PACKAGES = {
            "com.android.dialer", "com.android.contacts", "com.google.android.dialer",
            "com.android.mms", "com.google.android.apps.messaging", "com.huawei.contacts", "com.huawei.message"
    };

    private PolicyGate() {
    }

    static JSONObject evaluateTap(AgentAccessibilityService service, float x, float y) throws Exception {
        if (CommerceAuthorization.ownerInteractionActive) {
            return block("tap_screen", "Only the owner may operate the authorization or identity verification screen.", null);
        }
        JSONObject node = service.describeNodeAt(x, y);
        JSONObject screen = service.policyScreenSummary();
        node.put("screen_text", screen.optString("screen_text", ""));
        node.put("active_package", screen.optString("package_name", ""));
        if (isOwnMediaProjectionConsent(node, screen, service.ownApplicationLabel())) {
            return allow("tap_screen", "medium", node);
        }
        // Accessibility observations are evidence, not a prerequisite for visual taps.
        // Only checkout commits invoke the owner's configurable confirmation flow.
        boolean approved = requiresCheckoutConsent(targetEvidenceText(node))
                && CommerceAuthorization.consumeApproval(service, node);
        return evaluateTapEvidence(node, approved);
    }

    static JSONObject evaluateTapEvidence(JSONObject node, boolean approved) throws Exception {
        if (CommerceAuthorization.ownerInteractionActive) {
            return block("tap_screen", "Only the owner may operate the authorization or identity verification screen.", node);
        }
        if (requiresCheckoutConsent(targetEvidenceText(node))) {
            if (approved) {
                return allow("tap_screen", "high", node)
                        .put("authorization_source", "local_owner");
            }
            return JsonUtil.obj("allowed", false, "status", "awaiting_owner_confirmation",
                    "action", "tap_screen", "risk_level", "high", "confirmation_required", true,
                    "evidence", node, "message_for_user", "付款或下单需要你在手机上授权，尚未执行。授权后请重新核对页面再继续。");
        }
        return allow("tap_screen", "medium", node);
    }

    static JSONObject evaluateSwipe(AgentAccessibilityService service) throws Exception {
        if (CommerceAuthorization.ownerInteractionActive) {
            return block("swipe_screen", "Only the owner may operate the authorization or identity verification screen.", null);
        }
        JSONObject screen = service.policyScreenSummary();
        return allow("swipe_screen", "medium", screen);
    }

    static JSONObject evaluateInputText(AgentAccessibilityService service) throws Exception {
        if (CommerceAuthorization.ownerInteractionActive) {
            return block("input_text", "Only the owner may operate the authorization or identity verification screen.", null);
        }
        JSONObject focused = service.focusedInputSummary();
        JSONObject screen = service.policyScreenSummary();
        focused.put("screen_text", screen.optString("screen_text", ""));
        focused.put("active_package", screen.optString("package_name", ""));
        return allow("input_text", "medium", focused);
    }

    static JSONObject evaluateOpenUrl(Context context, Uri uri) {
        if (CommerceAuthorization.ownerInteractionActive) {
            return block("open_url", "Only the owner may operate the authorization or identity verification screen.", null);
        }
        return allow("open_url", "low", new JSONObject());
    }

    static boolean isBrowserUrlLaunchingEnabled() {
        return true;
    }

    // The client's exact chat composer is not a payment form. Conversation text
    // can discuss payment without authorizing an external financial action.
    // Keep every other input, tap, order and payment gate unchanged.
    static boolean isOwnMessageComposer(
            String ownPackage, String screenPackage, String inputPackage,
            String viewId, String className, boolean verifiedEditableInput, boolean safeScreen) {
        return ("com.termux".equals(ownPackage) || "com.cheby.codex.mobile".equals(ownPackage))
                && ownPackage.equals(screenPackage) && ownPackage.equals(inputPackage)
                && "message_composer".equals(viewId)
                && "android.widget.EditText".equals(className)
                && verifiedEditableInput && safeScreen;
    }

    static JSONObject evaluateOpenApp(String packageName) {
        if (CommerceAuthorization.ownerInteractionActive) {
            return block("open_app", "Only the owner may operate the authorization or identity verification screen.", null);
        }
        return allow("open_app", "low", JsonUtil.obj("package_name", packageName));
    }

    static boolean isBrowserPackage(String packageName) {
        String normalized = packageName == null ? "" : packageName.toLowerCase(Locale.ROOT);
        return containsAny(normalized, BROWSER_PACKAGE_TERMS);
    }

    private static JSONObject allow(String action, String riskLevel, JSONObject evidence) {
        return JsonUtil.obj("allowed", true, "action", action, "risk_level", riskLevel,
                "confirmation_required", false, "evidence", evidence == null ? new JSONObject() : evidence);
    }

    private static JSONObject block(String action, String reason, JSONObject evidence) {
        return JsonUtil.obj("allowed", false, "status", "blocked", "action", action, "risk_level", "high",
                "confirmation_required", false, "policy_reason", reason,
                "evidence", evidence == null ? new JSONObject() : evidence,
                "message_for_user", "ChebyNode safety policy blocked this action; review policy_reason.");
    }

    static boolean requiresCheckoutConsent(String targetEvidence) {
        return isCheckoutPayment(targetEvidence) || hasTapConsequentialCommitRisk(targetEvidence);
    }

    static boolean containsFinancialRiskTerm(String value) {
        String normalized = value == null ? "" : value.toLowerCase(Locale.ROOT);
        return containsAny(normalized, FINANCIAL_RISK_TERMS)
                || containsWordToken(normalized, DEBT_WORDS)
                || containsFinancialPackageToken(normalized);
    }

    // Standing owner consent covers checkout, not transfers, lending or banking.
    static boolean isCheckoutPayment(String targetEvidence) {
        String text = targetEvidence == null ? "" : targetEvidence.toLowerCase(Locale.ROOT);
        if (containsAny(text, new String[] {"transfer", "loan", "credit", "borrow", "mortgage",
                "转账", "贷款", "借款", "分期", "信用", "кредит", "займ", "ипотек", "перевод", "рассроч"})) {
            return false;
        }
        return containsAny(text, new String[] {"pay now", "confirm payment", "complete payment",
                "立即支付", "确认支付", "确认付款", "立即付款", "оплатить", "подтвердить оплату"});
    }

    // Package fragments remain conservative in package-only gates. In arbitrary UI
    // text, however, "bank" in Embankment or "boc" in Bubochka is not bank evidence.
    private static boolean containsFinancialPackageToken(String value) {
        return containsWordToken(value, FINANCIAL_PACKAGE_TERMS);
    }

    private static boolean containsWordToken(String value, String[] terms) {
        for (String term : terms) {
            int from = 0;
            int start;
            while ((start = value.indexOf(term, from)) >= 0) {
                int end = start + term.length();
                boolean leftBoundary = start == 0 || !Character.isLetterOrDigit(value.charAt(start - 1));
                boolean rightBoundary = end == value.length() || !Character.isLetterOrDigit(value.charAt(end));
                if (leftBoundary && rightBoundary) return true;
                from = end;
            }
        }
        return false;
    }

    private static boolean hasTapFinancialRisk(JSONObject node) {
        return hasTapFinancialRisk(
                targetEvidenceText(node),
                node == null ? "" : node.optString("screen_text", ""));
    }

    static boolean hasTapFinancialRisk(String targetEvidence, String screenTextForAuditOnly) {
        return containsFinancialRiskTerm(targetEvidence);
    }

    private static boolean hasTapConsequentialCommitRisk(JSONObject node) {
        return hasTapConsequentialCommitRisk(targetEvidenceText(node));
    }

    static boolean hasTapConsequentialCommitRisk(String targetEvidence) {
        String normalized = targetEvidence == null ? "" : targetEvidence.toLowerCase(Locale.ROOT);
        return containsAny(normalized, CONSEQUENTIAL_COMMIT_TERMS);
    }

    private static boolean hasTapBrowserLaunchRisk(JSONObject node) {
        return hasTapBrowserLaunchRisk(targetEvidenceText(node));
    }

    static boolean hasTapBrowserLaunchRisk(String targetEvidence) {
        String normalized = targetEvidence == null ? "" : targetEvidence.toLowerCase(Locale.ROOT);
        return containsAny(normalized, BROWSER_LAUNCH_TERMS);
    }

    static boolean containsCallSmsAuthorizationTerm(String value) {
        String normalized = value == null ? "" : value.toLowerCase(Locale.ROOT);
        return containsAny(normalized, CALL_TERMS)
                || containsStandaloneCallAction(normalized)
                || (containsAny(normalized, SMS_CONTEXT_TERMS) && containsAny(normalized, SMS_SEND_TERMS));
    }

    private static boolean containsStandaloneCallAction(String normalized) {
        for (String line : normalized.split("\\R")) {
            String clean = line.trim();
            if (clean.equals("call") || clean.startsWith("call ")) return true;
        }
        return false;
    }

    static boolean hasCallSmsTapRisk(JSONObject node, JSONObject screen) {
        String packageName = screen == null ? "" : screen.optString("package_name", "");
        return hasCallSmsTapRisk(targetEvidenceText(node), packageName);
    }

    static boolean hasCallSmsTapRisk(String targetEvidence, String packageName) {
        return isCallSmsMutationPackage(packageName) || containsCallSmsAuthorizationTerm(targetEvidence);
    }

    static boolean isOwnMediaProjectionConsent(
            JSONObject node,
            JSONObject screen,
            String expectedAppLabel) {
        if (node == null || screen == null) return false;
        return isOwnMediaProjectionConsent(
                screen.optString("package_name", ""),
                node.optString("package_name", ""),
                node.optBoolean("found", false),
                node.optBoolean("clickable", false),
                node.optString("class_name", ""),
                node.optString("view_id", ""),
                node.optString("text", ""),
                screen.optString("screen_text", ""),
                expectedAppLabel
        );
    }

    static boolean isOwnMediaProjectionConsent(
            String screenPackage,
            String nodePackage,
            boolean found,
            boolean clickable,
            String nodeClass,
            String viewId,
            String text,
            String screenText,
            String expectedAppLabel) {
        String exactQuestion = "是否允许“" + expectedAppLabel + "”录制/投射您的屏幕";
        return "com.android.systemui".equals(screenPackage)
                && "com.android.systemui".equals(nodePackage)
                && expectedAppLabel != null
                && !expectedAppLabel.isEmpty()
                && found
                && clickable
                && "android.widget.Button".equals(nodeClass)
                && "android:id/button1".equals(viewId)
                && "允许".equals(text)
                && screenText.contains(exactQuestion)
                && screenText.contains("android:id/button2")
                && screenText.contains("禁止");
    }

    static boolean preferTapTarget(
            boolean hasCurrent,
            boolean currentHasSemantics,
            int currentDepth,
            int currentArea,
            boolean candidateHasSemantics,
            int candidateDepth,
            int candidateArea) {
        if (!hasCurrent) return true;
        if (candidateHasSemantics != currentHasSemantics) return candidateHasSemantics;
        return candidateDepth > currentDepth
                || (candidateDepth == currentDepth && candidateArea < currentArea);
    }

    static boolean isCallSmsMutationPackage(String packageName) {
        String normalized = packageName == null ? "" : packageName.toLowerCase(Locale.ROOT);
        return containsAny(normalized, CALL_SMS_MUTATION_PACKAGES);
    }

    private static boolean containsAny(String value, String[] terms) {
        for (String term : terms) {
            if (value.contains(term.toLowerCase(Locale.ROOT))) return true;
        }
        return false;
    }

    static boolean isProtectedMutationPackage(String packageName) {
        String normalized = packageName == null ? "" : packageName.toLowerCase(Locale.ROOT);
        return containsAny(normalized, PROTECTED_MUTATION_PACKAGES)
                || containsAny(normalized, FINANCIAL_PACKAGE_TERMS);
    }

    static boolean hasSafeScreenContext(boolean found, String packageName, boolean hasSemantics) {
        return found && packageName != null && !packageName.trim().isEmpty() && hasSemantics;
    }

    static String authoritativeScreenPackage(String rootPackage, String eventPackage) {
        String root = rootPackage == null ? "" : rootPackage.trim();
        if (!root.isEmpty()) return root;
        return eventPackage == null ? "" : eventPackage.trim();
    }

    private static boolean hasActionSemantics(JSONObject evidence) {
        return !evidence.optString("text", "").trim().isEmpty()
                || !evidence.optString("content_description", "").trim().isEmpty()
                || !evidence.optString("view_id", "").trim().isEmpty();
    }

    private static String evidenceText(JSONObject evidence) {
        if (evidence == null) return "";
        return evidence.optString("text", "") + "\n" + evidence.optString("content_description", "") + "\n"
                + evidence.optString("view_id", "") + "\n" + evidence.optString("package_name", "") + "\n"
                + evidence.optString("active_package", "") + "\n" + evidence.optString("class_name", "") + "\n"
                + evidence.optString("screen_text", "");
    }

    private static String targetEvidenceText(JSONObject evidence) {
        if (evidence == null) return "";
        return evidence.optString("text", "") + "\n" + evidence.optString("content_description", "") + "\n"
                + evidence.optString("view_id", "") + "\n" + evidence.optString("package_name", "") + "\n"
                + evidence.optString("active_package", "") + "\n" + evidence.optString("class_name", "");
    }
}
