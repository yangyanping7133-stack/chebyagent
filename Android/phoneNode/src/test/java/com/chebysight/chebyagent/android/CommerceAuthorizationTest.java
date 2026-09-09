package com.chebysight.chebyagent.android;

import org.junit.Test;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public final class CommerceAuthorizationTest {
    @Test
    public void oneShotConsentRequiresSameScreenAndUnexpiredDeadline() {
        assertTrue(CommerceAuthorization.approvalMatches("screen-a", "screen-a", 120, 119));
        assertFalse(CommerceAuthorization.approvalMatches(null, "screen-a", 120, 119));
        assertFalse(CommerceAuthorization.approvalMatches("screen-a", "screen-b", 120, 119));
        assertFalse(CommerceAuthorization.approvalMatches("screen-a", "screen-a", 120, 120));
        assertFalse(CommerceAuthorization.approvalMatches("screen-a", "screen-a", 120, 121));
    }

    @Test
    public void checkoutConsentIsNotLendingOrTransferConsent() {
        for (String target : new String[] {"Pay now", "确认付款", "Оплатить 500 ₽"}) {
            assertTrue(target, PolicyGate.isCheckoutPayment(target));
        }
        for (String target : new String[] {"Payment method", "credit card", "transfer pay now",
                "立即支付 贷款", "Оплатить кредит", "подтвердить перевод", "Москва и область", ""}) {
            assertFalse(target, PolicyGate.isCheckoutPayment(target));
        }
    }
}
