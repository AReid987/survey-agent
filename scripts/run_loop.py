#!/usr/bin/env python3
"""Persistent survey agent runner with randomized pauses.

Runs the survey-agent in a loop, detecting completions from logs.
Inserts randomized pauses between cycles:
- Target 8-12 surveys per cycle (randomly chosen)
- Pause 15-45 minutes between cycles (randomly chosen)
"""

import random
import subprocess
import time
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
LOG_FILE = Path(__file__).resolve().parent.parent / "data" / "runner.log"


def log(msg: str) -> None:
    """Log to both stdout and file."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def run_survey_cycle() -> int:
    """Run one survey-agent session. Returns number of surveys completed."""
    log("Starting survey-agent run...")
    try:
        result = subprocess.run(
            ["pdm", "run", "survey-agent", "run", "--backend", "browser-harness"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        output = result.stdout + result.stderr
        # Count orchestrator "Survey N completed" lines and points
        import re

        completed = len(
            re.findall(r"survey_agent\.orchestrator: \[Session \d+\] Survey \d+ (?:completed|COMPLETED)", output)
        )
        errors = len(re.findall(r"survey_agent\.orchestrator: \[Session \d+\] Survey failed", output))
        points_match = re.search(r"earned (\d+) points", output)
        points = int(points_match.group(1)) if points_match else 0

        log(f"Run finished: {completed} completed, {errors} errors, {points} points earned")
        log(f"Output tail: {output[-500:]}")

        # Restart Chrome between runs to prevent tab/renderer accumulation
        subprocess.run(["pkill", "-f", "remote-debugging-port=19333"], capture_output=True, check=False)
        subprocess.run(["pkill", "-f", "browser_harness.daemon"], capture_output=True, check=False)
        # Clean up stale lock files
        lock_file = Path.home() / "survey-agent" / "data" / "chrome_agent" / "SingletonLock"
        lock_file.unlink(missing_ok=True)
        time.sleep(3)

        return completed

    except subprocess.TimeoutExpired:
        log("Run timed out (15 min)")
        subprocess.run(["pkill", "-f", "remote-debugging-port=19333"], capture_output=True, check=False)
        subprocess.run(["pkill", "-f", "browser_harness.daemon"], capture_output=True, check=False)
        time.sleep(2)
        return 0
    except Exception as e:
        log(f"Run error: {e}")
        return 0


def main() -> None:
    """Main loop — runs forever with randomized pauses."""
    log("=" * 60)
    log("Survey Agent Runner — starting persistent loop")
    log("=" * 60)

    total_completed = 0
    cycle = 0

    while True:
        cycle += 1
        target_surveys = random.randint(8, 12)
        pause_minutes = random.randint(15, 45)

        log(f"Cycle {cycle}: target {target_surveys} surveys, then {pause_minutes} min pause")

        cycle_completed = 0
        for i in range(target_surveys):
            log(f"  Survey {i + 1}/{target_surveys}...")
            completed = run_survey_cycle()
            cycle_completed += completed
            total_completed += completed

            if completed > 0:
                log(f"  ✓ Survey completed! (cycle total: {cycle_completed}, all-time: {total_completed})")
            else:
                log(f"  ✗ No completion (cycle total: {cycle_completed})")

            # Brief pause between individual surveys
            time.sleep(random.randint(3, 8))

        log(f"Cycle {cycle} done: {cycle_completed} surveys completed")
        log(f"Pausing for {pause_minutes} minutes...")
        time.sleep(pause_minutes * 60)


if __name__ == "__main__":
    main()
