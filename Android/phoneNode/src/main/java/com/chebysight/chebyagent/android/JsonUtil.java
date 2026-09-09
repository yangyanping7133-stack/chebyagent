package com.chebysight.chebyagent.android;

import org.json.JSONArray;
import org.json.JSONObject;

final class JsonUtil {
    private JsonUtil() {
    }

    static JSONObject obj(Object... pairs) {
        JSONObject output = new JSONObject();
        try {
            for (int i = 0; i + 1 < pairs.length; i += 2) {
                output.put(String.valueOf(pairs[i]), pairs[i + 1]);
            }
        } catch (Exception ignored) {
        }
        return output;
    }

    static JSONArray arr(Object... values) {
        JSONArray output = new JSONArray();
        for (Object value : values) output.put(value);
        return output;
    }
}
