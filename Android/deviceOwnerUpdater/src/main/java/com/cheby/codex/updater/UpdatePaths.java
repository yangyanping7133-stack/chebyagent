package com.cheby.codex.updater;

import android.content.Context;

import java.io.File;
import java.io.IOException;

final class UpdatePaths {
    private UpdatePaths() {}

    static File ensureUpdatesDirectory(Context context) throws IOException {
        File externalRoot = context.getExternalFilesDir(null);
        if (externalRoot == null) {
            throw new IOException("private external storage is unavailable");
        }
        File updatesDirectory = new File(externalRoot, "updates");
        if (!updatesDirectory.isDirectory() && !updatesDirectory.mkdirs()) {
            throw new IOException("private update directory is unavailable");
        }
        File canonicalRoot = externalRoot.getCanonicalFile();
        File canonicalUpdates = updatesDirectory.getCanonicalFile();
        if (!canonicalRoot.equals(canonicalUpdates.getParentFile())) {
            throw new IOException("private update directory escaped its application root");
        }
        return canonicalUpdates;
    }
}
