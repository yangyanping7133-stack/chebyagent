package com.chebysight.chebyagent.android;

final class AccessibilityServiceList {
    private AccessibilityServiceList() {
    }

    static String add(String current, String requested) {
        if (requested == null || requested.trim().isEmpty()) {
            throw new IllegalArgumentException("requested accessibility service is required");
        }

        StringBuilder result = new StringBuilder();
        String requestedComponent = canonicalComponent(requested.trim());
        boolean requestedPresent = false;
        if (current != null && !current.trim().isEmpty()) {
            for (String item : current.split(":")) {
                String value = item.trim();
                if (value.isEmpty()) continue;
                append(result, value);
                if (requestedComponent.equals(canonicalComponent(value))) {
                    requestedPresent = true;
                }
            }
        }
        if (!requestedPresent) append(result, requested.trim());
        return result.toString();
    }

    private static void append(StringBuilder result, String value) {
        if (result.length() > 0) result.append(':');
        result.append(value);
    }

    static String remove(String current, String requested) {
        if (requested == null || requested.trim().isEmpty()) {
            throw new IllegalArgumentException("requested accessibility service is required");
        }
        String expected = canonicalComponent(requested.trim());
        StringBuilder result = new StringBuilder();
        if (current != null) {
            for (String item : current.split(":")) {
                String value = item.trim();
                if (!value.isEmpty() && !expected.equals(canonicalComponent(value))) {
                    append(result, value);
                }
            }
        }
        return result.toString();
    }

    private static String canonicalComponent(String value) {
        int separator = value.indexOf('/');
        if (separator <= 0 || separator == value.length() - 1) return value;
        String packageName = value.substring(0, separator);
        String className = value.substring(separator + 1);
        if (className.startsWith(".")) className = packageName + className;
        return packageName + "/" + className;
    }
}
