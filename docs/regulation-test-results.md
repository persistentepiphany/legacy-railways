# Regulation test — §5 corridor cases

Generated: 2026-06-28. Compares the feed-side classifier (`tools/classify_corridor.py`) against the BRFares legacy JSON oracle (`tools/fetch_brfares.py`).

| Case | Ticket | Feed fare (p) | BRFares fare (p) | Regulated? | §5 expects | Rule fired | MATCH? |
|---|---|---:|---:|---|---|---|---|
| MAN<->EUS Off-Peak Return | `SVR` | 5650 | — | Regulated | Regulated | §1: Off-Peak Return on long-distance flow + Std class | ⚠️ pending |
| SOT<->MAN Off-Peak Return | `SVR` | 2220 | — | Regulated | Regulated | §1: Off-Peak Return on long-distance flow + Std class | ⚠️ pending |
| MAN<->EUS Anytime Return | `SOR` | 14000 | — | NOT regulated | NOT regulated | §1: Standard walk-up not on regulated list (e.g. anytime single) | ⚠️ pending |
| MAN<->EUS Advance | `C1S` | 3600 | — | NOT regulated | NOT regulated | §1: Advance fare (.TTY DESCRIPTION says ADVANCE) | ⚠️ pending |
| MAN<->EUS First Class Rtn | `FOR` | — | — | MISSING | NOT regulated | ticket not on this corridor in .FFL — honest gap, not a guess | ⚠️ pending |

## Sources
- **Classifier** — `data/classification_corridor.json` (mtime 2026-06-28T19:32:31)
- **BRFares MAN-EUS** — _missing_; re-run the producer script.
- **BRFares SOT-MAN** — _missing_; re-run the producer script.
