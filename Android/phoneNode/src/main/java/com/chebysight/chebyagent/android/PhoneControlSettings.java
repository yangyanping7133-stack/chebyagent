package com.chebysight.chebyagent.android;

import android.content.Context;
import android.content.SharedPreferences;

final class PhoneControlSettings {
    private static final String PREFS = "cheby_phone_node_settings";
    private static final String KEY_CALL_SMS_ALLOWED = "call_sms_allowed";

    private PhoneControlSettings() {
    }

    static boolean isCallSmsAllowed(Context context) {
        return prefs(context).getBoolean(KEY_CALL_SMS_ALLOWED, false);
    }

    private static SharedPreferences prefs(Context context) {
        return context.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }
}
