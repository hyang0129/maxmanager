from pathlib import Path

# Profile storage — bind-mounted from the Windows host
PROFILES_DIR: Path = Path.home() / ".claude-profiles"

# Active credential destination — VS Code extension always reads this path
ACTIVE_CREDS: Path = Path.home() / ".claude" / ".credentials.json"

# Persistent state written by the selector loop
STATE_FILE: Path = Path("/tmp/maxmanager_state.json")

# --- Switching thresholds ---

# 7-day delta required to trigger a weekly equalization switch
DELTA_7D_THRESHOLD: int = 15

# 5-hour utilization fraction at which the soft-ceiling trigger fires
SOFT_CEILING_5HR: float = 0.90

# Additional 7-day delta required when a switch would reverse the last
# swap direction (anti-thrash premium)
DIRECTION_REVERSAL_PREMIUM: int = 25

# --- Timing ---

# Minimum hours between consecutive switches (cooldown guard)
COOLDOWN_HOURS: int = 2

# Minutes before the 5-hour window resets at which the 5hr trigger is
# suppressed (wait for the reset instead of switching)
RESET_PROXIMITY_MINUTES: int = 30

# Base interval between choose_credential() loop iterations, in seconds
LOOP_INTERVAL_SECONDS: int = 3600

# Maximum random jitter added to each loop sleep, in seconds
LOOP_JITTER_SECONDS: int = 120
