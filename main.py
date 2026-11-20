#!/usr/bin/env python3
# -*- coding:utf-8 -*-
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import ATRI
from ATRI.config import ENDPOINT_ENV, BotSelfConfig, endpoint_configs


def run_endpoint() -> None:
    ATRI.init()
    ATRI.run()


def _stop_on_sigterm(signum, frame):
    raise SystemExit(128 + signum)


def main() -> int:
    if ENDPOINT_ENV in os.environ:
        run_endpoint()
        return 0

    enabled = [name for name, config in endpoint_configs.items() if config.enabled]
    if not enabled:
        print("没有启用的接入点。")
        return 0
    if len(enabled) == 1:
        run_endpoint()
        return 0

    signal.signal(signal.SIGTERM, _stop_on_sigterm)
    workers = {}
    exit_status = 0
    interrupted = False
    try:
        for name in enabled:
            print(f"启动接入点 {name}", flush=True)
            workers[name] = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve())],
                env={**os.environ, ENDPOINT_ENV: name},
            )
        while workers:
            for name, worker in list(workers.items()):
                code = worker.poll()
                if code is not None:
                    print(f"接入点 {name} 已退出（状态 {code}）", flush=True)
                    exit_status |= int(code != 0)
                    del workers[name]
            if workers:
                time.sleep(0.5)
    except KeyboardInterrupt:
        # Console Ctrl+C is also delivered to the child processes.
        interrupted = True
    finally:
        if not interrupted:
            for worker in workers.values():
                if worker.poll() is None:
                    worker.terminate()
        deadline = time.monotonic() + 10
        for worker in workers.values():
            try:
                worker.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
elif BotSelfConfig.has_listener:
    ATRI.init()
    app = ATRI.asgi()
