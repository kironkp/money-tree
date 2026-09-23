# Forex repair — protocol, fixed before any result is looked at

## The diagnosis this is attacking
Round-trip cost is a flat ~1.5 bps. The strategies capture 0.6-1.6 bps per trade.
The edge is real but the same size as the toll. Every candidate below must make
the CAPTURED MOVE bigger relative to a fixed cost. Anything that only trades
less is not a fix, it is a slower way of stopping.

## Data
1Hour bars, 4 USD majors, 2024-09-08 -> 2026-09-07 (~12,300 bars/pair).
15Min has only 2.5 months and cannot support a split; it is reported as a
secondary, underpowered readout only and may not decide anything.

## Split — fixed now, before any result is seen
- TRAIN 2024-09-08 -> 2025-12-31   (search freely)
- TEST  2026-01-01 -> 2026-09-07   (ONE look, by ONE chosen candidate per strategy)

## Candidates — mechanism stated, list closed
- C0  baseline params on 1Hour
- C1  min_reward_to_cost: refuse a trade whose target cannot clear the toll
- C2  stop_atr_mult wider: a bigger stop buys a proportionally bigger target
- C3  rr wider (ema_momentum): aim further for the same risk
- C4  entry_z deeper (vwap_reversion): only take stretched setups, which revert further
- C5  hold longer: max_hold / max_bars_held, so a move has time to arrive
- C6  explicit minimum target in bps: the diagnosis as a direct gate
- C7  timeframe: 1Hour vs 15Min, the structural version of "bigger moves"

## Decision rule — all four, on TEST, or it does not ship
1. mean daily net > 0 after every cost
2. circular-block-bootstrap 95% lower bound > 0
3. gross profit factor >= 1.10
4. captured bps >= 2x cost bps        <- the diagnosis, restated as a bar

A candidate that only passes on TRAIN is a failed candidate.
A parameter chosen by looking at TEST is a failed candidate.
If nothing passes, nothing ships and that is the finding.
