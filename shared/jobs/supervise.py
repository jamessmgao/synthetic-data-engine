"""Run several commands in one container and exit when any of them exits.

worker-control runs the generation worker, the Job Service and the reconciler
together (the reconciler is pinned there). If one dies, this exits non-zero and
the container's restart policy restarts all of them; state is in the database,
so nothing is lost.

Usage: python -m shared.jobs.supervise "<cmd 1>" "<cmd 2>" …   (empty commands are skipped)
"""

import shlex
import signal
import subprocess
import sys
import time


def main(argv: list[str]) -> int:
    cmds = [c for c in argv if c.strip()]
    if not cmds:
        print("supervise: no commands", file=sys.stderr)
        return 2
    procs = [subprocess.Popen(shlex.split(c)) for c in cmds]
    for c, p in zip(cmds, procs):
        print(f"supervise: started pid {p.pid}: {c}", flush=True)

    def stop(*_):
        for p in procs:
            if p.poll() is None:
                p.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while all(p.poll() is None for p in procs):
        time.sleep(1)
    dead = next(p for p in procs if p.poll() is not None)
    print(f"supervise: pid {dead.pid} exited {dead.returncode}; stopping the rest", flush=True)
    stop()
    for p in procs:
        try:
            p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            p.kill()
    return dead.returncode or 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
