#!/usr/bin/env python3
"""Probe Claude's /usage command by running it in a virtual terminal via pexpect+pyte."""

import json
import os
import sys
import time

import pexpect
import pyte

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
TIMEOUT = 30


def run_usage_probe():
    # Virtual terminal: 120 cols x 40 rows
    screen = pyte.Screen(120, 40)
    stream = pyte.Stream(screen)

    # Spawn claude interactively with a PTY
    child = pexpect.spawn(
        CLAUDE_BIN,
        args=["--dangerously-skip-permissions"],
        encoding="utf-8",
        timeout=TIMEOUT,
        dimensions=(40, 120),
    )

    def feed_and_dump():
        """Read available output, feed to pyte, return screen text."""
        try:
            data = child.read_nonblocking(size=4096, timeout=1)
            stream.feed(data)
        except (pexpect.TIMEOUT, pexpect.EOF):
            pass
        lines = [screen.display[i].rstrip() for i in range(screen.lines)]
        return "\n".join(lines)

    def get_screen():
        return "\n".join(screen.display[i].rstrip() for i in range(screen.lines))

    print("[probe] Waiting for claude to start...", flush=True)

    # Wait for the bypass permissions prompt and accept it
    deadline = time.time() + TIMEOUT
    accepted = False
    while time.time() < deadline:
        feed_and_dump()
        text = get_screen()
        if "Yes, I accept" in text and not accepted:
            print("[probe] Accepting bypass permissions...", flush=True)
            # Press down arrow to select "Yes", then Enter
            child.send("\x1b[B")  # down arrow
            time.sleep(0.3)
            child.send("\r")  # enter
            accepted = True
            time.sleep(2)
            continue
        if accepted and (">" in text or "❯" in text or "Claude" in text):
            # Looks like we're at the prompt
            feed_and_dump()
            text = get_screen()
            # Check if it's really the input prompt (not still loading)
            if "tip" in text.lower() or "help" in text.lower() or ">" in text:
                break
        time.sleep(0.5)

    feed_and_dump()
    print("[probe] At prompt. Sending /usage...", flush=True)

    # Clear screen state and send /usage followed by Enter
    screen.reset()
    # Type it character by character then press Enter
    for ch in "/usage":
        child.send(ch)
        time.sleep(0.05)
    time.sleep(1)
    feed_and_dump()
    # The autocomplete menu may appear. Press Enter to execute.
    child.send("\r")

    # Wait for /usage output to render
    time.sleep(3)
    for _ in range(15):
        feed_and_dump()
        time.sleep(1)
        text = get_screen()
        if "%" in text or "limit" in text.lower() or "reset" in text.lower():
            break

    feed_and_dump()
    final_text = get_screen()
    print("[probe] Screen content after /usage:", flush=True)
    print("=" * 60)
    for line in final_text.split("\n"):
        if line.strip():
            print(line)
    print("=" * 60)

    # Parse usage percentages
    import re
    result = parse_usage_screen(final_text)
    print(f"\n[probe] Parsed: {json.dumps(result, indent=2)}", flush=True)

    # Write to file
    if result.get("five_hour_used_pct") is not None:
        out_path = "/tmp/maxmanager_rate_limits.json"
        result["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(out_path + ".tmp", "w") as f:
            json.dump(result, f, indent=2)
        os.rename(out_path + ".tmp", out_path)
        print(f"[probe] Written to {out_path}", flush=True)

    # Exit cleanly: press Esc first (to dismiss /usage), then /exit
    child.send("\x1b")  # Esc
    time.sleep(0.5)
    child.sendline("/exit")
    time.sleep(1)
    try:
        child.close()
    except Exception:
        pass

    return result


def parse_usage_screen(text: str) -> dict:
    """Extract usage percentages from the /usage screen output."""
    import re
    result = {
        "five_hour_used_pct": None,
        "five_hour_resets_at": None,
        "seven_day_used_pct": None,
        "seven_day_resets_at": None,
        "sonnet_week_used_pct": None,
        "sonnet_week_resets_at": None,
    }

    lines = text.split("\n")
    for i, line in enumerate(lines):
        # Look for percentage patterns like "58% used" or "33% used"
        pct_match = re.search(r"(\d+)%\s*used", line)
        if not pct_match:
            continue
        pct = int(pct_match.group(1))

        # Look backwards for context (session vs week vs sonnet)
        context = ""
        for j in range(max(0, i - 3), i):
            context += lines[j].lower() + " "

        reset_match = None
        # Look forward for reset time
        for j in range(i, min(len(lines), i + 3)):
            rm = re.search(r"[Rr]eset[^\d]*([\w\d, :()]+)", lines[j])
            if rm:
                reset_match = rm.group(1).strip()
                break

        if "session" in context or "5" in context:
            if result["five_hour_used_pct"] is None:
                result["five_hour_used_pct"] = pct
                result["five_hour_resets_at"] = reset_match
        elif "sonnet" in context:
            result["sonnet_week_used_pct"] = pct
            result["sonnet_week_resets_at"] = reset_match
        elif "week" in context:
            if result["seven_day_used_pct"] is None:
                result["seven_day_used_pct"] = pct
                result["seven_day_resets_at"] = reset_match

    return result


if __name__ == "__main__":
    run_usage_probe()
