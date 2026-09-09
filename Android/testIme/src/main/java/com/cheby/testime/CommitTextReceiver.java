package com.cheby.testime;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Base64;
import java.nio.charset.StandardCharsets;

public final class CommitTextReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        String encoded = intent.getStringExtra("base64");
        if (encoded == null) {
            setResultCode(2);
            setResultData("missing base64");
            return;
        }
        try {
            String text = new String(Base64.decode(encoded, Base64.DEFAULT), StandardCharsets.UTF_8);
            boolean committed = ChebyTestImeService.replaceText(text);
            setResultCode(committed ? 0 : 3);
            setResultData(committed ? "committed" : "input connection unavailable");
        } catch (IllegalArgumentException exception) {
            setResultCode(4);
            setResultData("invalid base64");
        }
    }
}
