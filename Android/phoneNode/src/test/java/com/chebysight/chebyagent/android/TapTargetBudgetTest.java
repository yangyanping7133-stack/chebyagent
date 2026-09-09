package com.chebysight.chebyagent.android;

import org.junit.Test;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public final class TapTargetBudgetTest {
    @Test
    public void deeplyNestedControlsAreNotCutOffAtTen() {
        TapTargetBudget budget = new TapTargetBudget();
        for (int depth = 0; depth <= TapTargetBudget.MAX_DEPTH; depth++) {
            assertTrue("depth " + depth, budget.visit(depth));
        }
        assertFalse(budget.isTruncated());
        assertFalse(budget.visit(TapTargetBudget.MAX_DEPTH + 1));
        assertTrue(budget.isTruncated());
    }

    @Test
    public void wideTreesAreBoundedToo() {
        TapTargetBudget budget = new TapTargetBudget();
        for (int count = 0; count < TapTargetBudget.MAX_NODES; count++) assertTrue(budget.visit(1));
        assertFalse(budget.visit(1));
        assertTrue(budget.isTruncated());
    }
}
