package com.chebysight.chebyagent.android;

/** Allow deep Compose controls without an unbounded accessibility traversal. */
final class TapTargetBudget {
    static final int MAX_DEPTH = 32;
    static final int MAX_NODES = 4096;
    private int visited;
    private boolean truncated;

    boolean visit(int depth) {
        if (depth < 0 || depth > MAX_DEPTH || visited >= MAX_NODES) {
            truncated = true;
            return false;
        }
        visited++;
        return true;
    }

    boolean isTruncated() {
        return truncated;
    }
}
