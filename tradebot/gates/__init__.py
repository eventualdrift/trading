"""The gates that replace the owner's approvals (CLAUDE.md):

1. ``same_trades`` and ``deploy`` - a change deploys when lint, the full suite, the fresh-clone
   suite and the same-trades check pass; different orders need a passed registered test;
2. the research gate lives in ``tradebot.protocols`` (register, run once, record);
3. ``drawdown`` - limits set in advance from the backtest's own distribution;
4. reporting: ``tradebot notify`` and ``tradebot weekly --notify``.
"""
