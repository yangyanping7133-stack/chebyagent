package com.chebysight.chebyagent.android;

import android.Manifest;
import android.content.ComponentName;
import android.content.Context;
import android.content.pm.PackageManager;
import android.provider.Settings;
import android.text.TextUtils;

final class AccessibilityState {
    private AccessibilityState() {
    }

    static boolean isServiceEnabled(Context context) {
        if (Settings.Secure.getInt(
                context.getContentResolver(),
                Settings.Secure.ACCESSIBILITY_ENABLED,
                0
        ) != 1) {
            return false;
        }
        String enabled = Settings.Secure.getString(
                context.getContentResolver(),
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
        );
        if (TextUtils.isEmpty(enabled)) return false;

        String expected = new ComponentName(context, AgentAccessibilityService.class).flattenToString();
        ComponentName expectedComponent = ComponentName.unflattenFromString(expected);
        TextUtils.SimpleStringSplitter splitter = new TextUtils.SimpleStringSplitter(':');
        splitter.setString(enabled);
        while (splitter.hasNext()) {
            String service = splitter.next();
            ComponentName configured = ComponentName.unflattenFromString(service);
            if (expectedComponent != null && expectedComponent.equals(configured)) return true;
        }
        return false;
    }

    static boolean enableForManagedDevice(Context context) {
        if (context.checkSelfPermission(Manifest.permission.WRITE_SECURE_SETTINGS)
                != PackageManager.PERMISSION_GRANTED) {
            return false;
        }

        ComponentName component = new ComponentName(context, AgentAccessibilityService.class);
        String current = Settings.Secure.getString(
                context.getContentResolver(),
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
        );
        String merged = AccessibilityServiceList.add(current, component.flattenToString());
        try {
            return AccessibilitySettingsTransaction.apply(
                    new AccessibilitySettingsTransaction.Writer() {
                        @Override
                        public boolean writeEnabledServices(String value) {
                            return Settings.Secure.putString(
                                    context.getContentResolver(),
                                    Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
                                    value
                            );
                        }

                        @Override
                        public boolean enableAccessibility() {
                            return Settings.Secure.putInt(
                                    context.getContentResolver(),
                                    Settings.Secure.ACCESSIBILITY_ENABLED,
                                    1
                            );
                        }
                    },
                    merged
            );
        } catch (SecurityException ignored) {
            return false;
        }
    }

    static boolean removeOwnServiceForManagedRebind(Context context) {
        if (context.checkSelfPermission(Manifest.permission.WRITE_SECURE_SETTINGS)
                != PackageManager.PERMISSION_GRANTED) return false;
        String own = new ComponentName(context, AgentAccessibilityService.class).flattenToString();
        String current = Settings.Secure.getString(context.getContentResolver(),
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
        try {
            return Settings.Secure.putString(context.getContentResolver(),
                    Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
                    AccessibilityServiceList.remove(current, own));
        } catch (SecurityException ignored) {
            return false;
        }
    }
}
