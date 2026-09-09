package com.cheby.codex.updater;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;

public final class UpdateStatusProvider extends ContentProvider {
    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public Bundle call(String method, String argument, Bundle extras) {
        if (!"status".equals(method)) {
            throw new IllegalArgumentException("unsupported method");
        }
        Context context = getContext();
        if (context == null) {
            throw new IllegalStateException("provider context is unavailable");
        }
        UpdateStatusStore.Snapshot snapshot = UpdateStatusStore.read(context);
        Bundle result = new Bundle();
        result.putString("state", snapshot.state());
        result.putInt("session_id", snapshot.sessionId());
        result.putInt("status", snapshot.status());
        result.putLong("updated_at_ms", snapshot.updatedAtMs());
        return result;
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection,
                        String[] selectionArgs, String sortOrder) {
        throw new UnsupportedOperationException();
    }

    @Override
    public String getType(Uri uri) {
        return null;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        throw new UnsupportedOperationException();
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException();
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection,
                      String[] selectionArgs) {
        throw new UnsupportedOperationException();
    }
}
