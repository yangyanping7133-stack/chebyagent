package com.chebysight.chebyagent.android;

final class AccessibilitySettingsTransaction {
    interface Writer {
        boolean writeEnabledServices(String value);

        boolean enableAccessibility();
    }

    private AccessibilitySettingsTransaction() {
    }

    static boolean apply(Writer writer, String enabledServices) {
        if (!writer.writeEnabledServices(enabledServices)) return false;
        return writer.enableAccessibility();
    }
}
