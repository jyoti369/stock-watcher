"""Always-on alert scheduler for the deployed container.

Runs the watcher on a fixed interval forever. The per-rule cooldown in config
prevents alert spam, so a tight interval just means alerts land promptly. Locally
you'd use launchd/cron instead of this; on the container this is the worker.
"""
from __future__ import annotations

import os
import time

from . import watcher

INTERVAL = int(os.environ.get("STOCKWATCH_INTERVAL_SEC", 15 * 60))


def main() -> None:
    print(f"[scheduler] started · checking every {INTERVAL}s", flush=True)
    while True:
        try:
            watcher.run_once(verbose=True)
        except Exception as e:                       # never let the loop die
            print(f"[scheduler] error: {e!r}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
