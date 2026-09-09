package com.cheby.testime;

import android.inputmethodservice.InputMethodService;
import android.view.View;
import android.view.inputmethod.InputConnection;
import android.widget.TextView;

public final class ChebyTestImeService extends InputMethodService {
    private static volatile ChebyTestImeService instance;

    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;
    }

    @Override
    public void onDestroy() {
        if (instance == this) {
            instance = null;
        }
        super.onDestroy();
    }

    @Override
    public View onCreateInputView() {
        TextView view = new TextView(this);
        view.setText("Cheby private-test input");
        view.setTextSize(12f);
        view.setPadding(24, 12, 24, 12);
        return view;
    }

    static boolean replaceText(String text) {
        ChebyTestImeService service = instance;
        if (service == null) {
            return false;
        }
        InputConnection connection = service.getCurrentInputConnection();
        if (connection == null) {
            return false;
        }
        connection.beginBatchEdit();
        try {
            connection.deleteSurroundingText(100_000, 100_000);
            return connection.commitText(text, 1);
        } finally {
            connection.endBatchEdit();
        }
    }
}
