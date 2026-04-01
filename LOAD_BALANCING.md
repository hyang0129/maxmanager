# maxmanager Load Balancing Design

## Rate Limit Windows

Each Claude Max account has two independent rate limit windows:

- **5-hour window**: tracks recent usage. Exhaustion causes a temporary lockout (~5h) until the window resets. Does not affect the 7-day budget.
- **7-day window**: tracks cumulative weekly usage. Exhaustion causes a full week-long lockout. Losing an account for a week also loses its 5-hour capacity for that period — reducing peak throughput to N-1 credentials.

These windows are independent. Hitting the 5-hour ceiling does not consume extra 7-day budget.

## Two Separate Concerns

This distinction means the two windows drive switching for different reasons:

| Signal | Why we switch | Severity |
|---|---|---|
| 7-day delta between accounts | Equalize weekly burn so neither account ever hits the 7-day ceiling | Catastrophic if ignored |
| 5-hour utilization ≥ 90% | Avoid interrupting in-progress work | Disruptive but recoverable |

These are not combined into a single score. They are two independent reasons to switch.

## Switching Logic

### Trigger 1: 7-Day Equalization (primary)

Switch when the current account's 7-day utilization is meaningfully higher than the best alternative.

```
if current.7d - best_other.7d >= DELTA_7D_THRESHOLD:
    consider switching to best_other
```

**Goal**: keep all accounts aging at roughly the same weekly rate. With N=2, this produces natural alternation — use A until A's 7d is ahead of B by the threshold, switch to B, repeat.

**Why this prevents catastrophe**: if both accounts track each other's 7d usage, neither will reach 100% significantly before the other. The worst case is both accounts approaching the 7d ceiling together, which at least preserves equal capacity.

### Trigger 2: 5-Hour Soft Ceiling (secondary)

Switch when the current account's 5-hour utilization reaches the soft ceiling, if a better option exists.

```
if current.5hr >= 0.90 and best_other.5hr < current.5hr:
    consider switching to best_other
```

**Goal**: avoid locking out the current container mid-session. This is purely a work-continuity concern — it has no bearing on the 7-day budget.

**If all accounts are above the soft ceiling**: switch to the account with the lowest 5-hour utilization anyway. A lockout is coming regardless; switching at least delays it slightly and may spread the 5-hour impact across accounts.

### No Switch

If neither trigger fires, stay on the current account.

## Guards (applied after triggers)

These apply regardless of which trigger fires:

| Guard | Behaviour |
|---|---|
| CLI is active (I/O, child procs, TCP :443) | Never switch — would not take effect mid-session anyway, and is unnecessary disruption |
| Cooldown: < 2h since last switch | Skip — prevent thrash |
| Direction reversal | Require 25-point higher 7d delta to switch back to the last-swapped-from account |
| 5hr reset imminent (< 30 min) | Skip 5hr trigger — the window is about to clear; wait instead |

## Startup Behaviour

On first run, guards are bypassed — there is no cooldown, no direction to reverse, and no running session to disrupt. The algorithm applies both triggers normally and activates the best credential immediately.

## N=2 Expected Behaviour

With two credentials:

- **Normal usage**: accounts alternate as 7d delta crosses the threshold. Both accounts accumulate 7d usage at similar rates.
- **Burst usage**: 5hr ceiling may fire more frequently, causing faster alternation. 7d usage still accumulates on both accounts in parallel.
- **Both accounts near 7d ceiling**: the algorithm continues to pick the lower-7d account, but cannot prevent approaching the ceiling if total usage is high enough. At this point the system is genuinely resource-constrained.
- **One account hits 7d = 100%**: that account is disqualified. All traffic routes to the remaining account until the 7d window resets.

## Threshold Values

| Parameter | Value | Rationale |
|---|---|---|
| 7d switch delta | 15 points | Enough to avoid constant switching; small enough to keep accounts in sync |
| 5hr soft ceiling | 90% | Leaves a buffer before hard lockout; adjust if switches feel too early or too late |
| Direction reversal premium | 25 points | Anti-thrash: requires a clear advantage to reverse direction |
| Cooldown | 2 hours | Prevents rapid oscillation after a switch |
| 5hr reset proximity skip | 30 minutes | Not worth switching if the window resets imminently |

These are starting values. Adjust based on observed switching frequency.

## Out of Scope

Load spreading across 3+ credentials is not addressed here. With 2 credentials the alternation model is sufficient. A more sophisticated distribution strategy (e.g. least-loaded assignment at startup, per-container affinity) would be needed for larger credential pools.
