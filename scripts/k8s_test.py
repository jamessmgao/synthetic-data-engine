"""Chaos test against the local K3s cluster (docs/KUBERNETES.md, docs/DEPLOY.md §8). Run: `make k8s-test`.

    python scripts/k8s_test.py [--tasks 2] [--timeout S] [--context C] [--kubeconfig F] [--dry-run]

The stack must be deployed in mock mode (SDE_MOCK_LLM=1). Kubernetes is used to inject
work and to break things; the checks read Postgres through the services' own env.

  1. preflight: every Deployment / StatefulSet in namespace sde at its desired replicas
  2. inject N mock tasks (scripts/inject_task.py in the control pod)
  3. force-delete (grace 0) the executor pod that holds a LEASED rollout
  4. restart postgres-0 while tasks are still open
  5. wait until every task is decided, then check:
     - every rollout slot was scored at most once (jobs_one_scored_slot), and every slot of
       an analyzed version exactly once;
     - the killed rollout was re-claimed (lease_token bumped) and settled once, as DONE;
     - no job is left LEASED or READY;
     - no `sde.job=*` container, and nothing running, in any pod's dind sidecar;
     - KEDA scaled executor above 1 replica during the run (sampled every 10 s); a
       scale-down is reported if it happens before the run ends.

Always passes an explicit context (default k3d-sde; on the AWS K3s server node:
--kubeconfig /etc/rancher/k3s/k3s.yaml --context default) and namespace sde, never the
current context. Test tasks
are named k8s-<run>-NNN. A killed pod's rollout is re-claimed once its lease expires; the
local overlay's short leases (SDE_LEASE_TTL_S=45, SDE_SUSPECT_MAX_WAIT_S=10) make that ~1 min.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CTX, NS, KUBECONFIG = "k3d-sde", "sde", None
FIXTURE = ROOT / "output" / "p1" / "p1-local-1" / "task"
POD_FIXTURE = "/tmp/k8s-test-fixture"
DECIDED = ("ACCEPTED", "REJECTED", "CANCELLED")
DRY = False


def step(msg: str) -> None:
    print(f"\n== {msg}", flush=True)


def check(ok: bool, msg: str) -> None:
    print(f"    {'ok  ' if ok else 'FAIL'} {msg}", flush=True)
    if not ok:
        raise SystemExit(1)


def kubectl(*args: str, stdin: str | None = None, check_rc: bool = True) -> str:
    cmd = ["kubectl", *(["--kubeconfig", KUBECONFIG] if KUBECONFIG else []), "--context", CTX, "-n", NS, *args]
    if DRY:
        print("    $", " ".join(cmd))
        return ""
    out = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if check_rc and out.returncode != 0:
        raise SystemExit(f"    FAIL {' '.join(cmd)}: {out.stderr.strip()}")
    return out.stdout


def control_pod() -> str:
    if DRY:
        return "<control pod>"
    return json.loads(kubectl("get", "pods", "-l", "app.kubernetes.io/name=control",
                              "--field-selector=status.phase=Running", "-o", "json"))["items"][0]["metadata"]["name"]


def sql(query: str, *params) -> list[dict]:
    """Rows of a query, run in the control pod against the services' backends."""
    code = ("import json, sys; from shared.storage import from_env; q, p = json.loads(sys.stdin.read()); "
            "print(json.dumps([dict(r) for r in from_env().store.conn.execute(q, p).fetchall()], default=str))")
    out = kubectl("exec", "-i", control_pod(), "--", "python", "-c", code, stdin=json.dumps([query, params]))
    return json.loads(out) if out else []


def wait_for(fn, timeout: float, what: str, every: float = 5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if (v := fn()):
                return v
        except SystemExit:
            pass  # e.g. Postgres or the pod is restarting
        time.sleep(every)
    raise SystemExit(f"    FAIL timed out after {timeout:.0f} s waiting for {what}")


def main() -> None:
    global DRY, CTX, KUBECONFIG
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--tasks", type=int, default=2)
    p.add_argument("--timeout", type=float, default=10 * 60, help="seconds to wait for every task to be decided")
    p.add_argument("--dind-container", default="dind", help="the Docker sidecar's container name")
    p.add_argument("--context", default=CTX)
    p.add_argument("--kubeconfig", help="default: kubectl's own (KUBECONFIG or ~/.kube/config)")
    p.add_argument("--task-dir", type=Path, default=FIXTURE, help="the task to inject (default: the P1 fixture)")
    p.add_argument("--dry-run", action="store_true", help="print the kubectl commands of each step; touch nothing")
    args = p.parse_args()
    DRY, CTX, KUBECONFIG = args.dry_run, args.context, args.kubeconfig
    check(DRY or args.task_dir.is_dir(), f"fixture {args.task_dir}")
    prefix = f"k8s-{time.strftime('%H%M%S')}-"
    like = prefix + "%"

    step("preflight: every Deployment / StatefulSet at its desired replicas, mock mode on")
    for d in [] if DRY else json.loads(kubectl("get", "deploy,statefulset", "-o", "json"))["items"]:
        want, have = d["spec"]["replicas"], d["status"].get("readyReplicas", 0)
        check(have == want, f"{d['kind']}/{d['metadata']['name']}: {have}/{want} ready")
    mock = kubectl("get", "configmap", "-l", "app.kubernetes.io/part-of=sde-pipeline", "-o", "json")
    check(DRY or any(c["data"].get("SDE_MOCK_LLM") == "1" for c in json.loads(mock)["items"]), "SDE_MOCK_LLM=1")

    replicas: list[tuple[float, int]] = []  # (seconds since start, executor .status.replicas)
    stop = threading.Event()
    t_start = time.time()

    def sample_replicas() -> None:
        while not stop.is_set():
            out = kubectl("get", "deploy", "executor", "-o", "jsonpath={.status.replicas}", check_rc=False)
            if out.strip().isdigit():
                replicas.append((time.time() - t_start, int(out)))
            stop.wait(10)

    if not DRY:
        threading.Thread(target=sample_replicas, daemon=True).start()

    step(f"inject {args.tasks} mock tasks ({prefix}NNN)")
    pod = control_pod()
    kubectl("exec", pod, "--", "rm", "-rf", POD_FIXTURE)
    kubectl("cp", str(args.task_dir), f"{pod}:{POD_FIXTURE}")
    for i in range(args.tasks):
        kubectl("exec", pod, "--", "python", "scripts/inject_task.py", POD_FIXTURE, f"{prefix}{i:03d}")
    print(f"    injected into {pod}")

    step("force-delete the executor pod holding a LEASED rollout")
    q_leased = "SELECT id, lease_owner, lease_token FROM jobs WHERE task_id LIKE ? AND type = 'rollout' AND status = 'LEASED'"
    victim = {"id": "<rollout job>", "lease_owner": "<pod>:<pid>", "lease_token": 1} if DRY else \
        wait_for(lambda: (sql(q_leased, like) or [None])[0], 600, "a leased rollout", every=2)
    victim_pod = victim["lease_owner"].split(":")[0]
    if not DRY:
        app = json.loads(kubectl("get", "pod", victim_pod, "-o", "json"))["metadata"]["labels"]["app.kubernetes.io/name"]
        check(app == "executor", f"{victim_pod} is an executor pod")
    kubectl("delete", "pod", victim_pod, "--grace-period=0", "--force")
    print(f"    killed {victim_pod} holding {victim['id']} (lease_token {victim['lease_token']})")

    step("restart postgres-0 while tasks are open")
    kubectl("delete", "pod", "postgres-0")
    kubectl("rollout", "status", "statefulset/postgres", "--timeout=300s")
    if not DRY:
        n = wait_for(lambda: sql("SELECT count(*) AS n FROM tasks WHERE id LIKE ?", like), 180, "Postgres back")
        check(n[0]["n"] == args.tasks, f"all {args.tasks} tasks still there after the restart")
    kubectl("rollout", "status", "deploy/executor", "--timeout=300s")
    if DRY:
        print("\nDRY RUN: nothing was changed")
        return

    step(f"wait until every task is decided (up to {args.timeout / 60:.0f} min)")
    t0 = time.time()
    q_open = f"SELECT count(*) AS n FROM tasks WHERE id LIKE ? AND status NOT IN {DECIDED}"
    wait_for(lambda: sql(q_open, like)[0]["n"] == 0, args.timeout, "every task decided", every=15)
    tasks = sql("SELECT id, status FROM tasks WHERE id LIKE ? ORDER BY id", like)
    summary = ", ".join(f"{t['id']} {t['status']}" for t in tasks)
    check(True, f"decided in {time.time() - t0:.0f} s: {summary}")

    step("exactly-once checks")
    dup = sql("""SELECT task_id, version, slot, count(*) AS n FROM jobs WHERE task_id LIKE ? AND type = 'rollout'
                 AND outcome IN ('VALID_PASS', 'VALID_FAIL') GROUP BY task_id, version, slot HAVING count(*) > 1""", like)
    check(not dup, f"no rollout slot scored twice {dup or ''}")
    # Every version the Trace Parser analyzed has each of its 5 slots scored exactly once.
    short = sql("""SELECT a.task_id, a.version, count(DISTINCT r.slot) AS slots FROM jobs a
                   LEFT JOIN jobs r ON r.task_id = a.task_id AND r.version = a.version AND r.type = 'rollout'
                        AND r.outcome IN ('VALID_PASS', 'VALID_FAIL')
                   WHERE a.task_id LIKE ? AND a.type = 'analyze' GROUP BY a.task_id, a.version""", like)
    check(bool(short) and all(s["slots"] == 5 for s in short), f"each analyzed version has 5 scored slots: {short}")
    k = sql("SELECT status, outcome, lease_token, attempts, finished_at FROM jobs WHERE id = ?", victim["id"])[0]
    check(k["status"] == "DONE" and k["lease_token"] > victim["lease_token"] and k["finished_at"],
          f"killed rollout {victim['id']} re-claimed and settled once: {k}")
    left = sql("SELECT id, type, status FROM jobs WHERE status IN ('LEASED', 'READY') AND task_id LIKE ?", like)
    check(not left, f"no job left LEASED or READY {left or ''}")

    step("no leaked containers in the dind sidecars")
    # Pod templates carry app.kubernetes.io/name, not part-of (that's on the Deployments only).
    pods = json.loads(kubectl("get", "pods", "-l", "app.kubernetes.io/name in (validate,executor,generation)",
                              "-o", "json"))["items"]
    # dind is a native sidecar (an initContainer with restartPolicy Always), not a container.
    runners = [r for r in pods if any(c["name"] == args.dind_container
                                      for c in r["spec"].get("containers", []) + r["spec"].get("initContainers", []))]
    check(bool(runners), f"{len(runners)} pods with a {args.dind_container} sidecar")
    for r in runners:
        name = r["metadata"]["name"]
        ps = lambda *f: kubectl("exec", name, "-c", args.dind_container, "--", "docker", "ps", "-q", *f).split()
        check(not ps("-a", "--filter", "label=sde.job"), f"{name}: no sde.job=* containers")
        check(not ps(), f"{name}: nothing running")

    step("KEDA scaled executor under the run's rollouts")
    stop.set()
    peak = max((n for _, n in replicas), default=0)
    t_peak = next((t for t, n in replicas if n == peak), 0)
    print("    executor replicas (s: n): " + ", ".join(
        f"{t:.0f}: {n}" for i, (t, n) in enumerate(replicas) if i == 0 or n != replicas[i - 1][1]))
    check(peak > 1, f"scaled up to {peak} replicas at {t_peak:.0f} s")
    last_t, last = replicas[-1]
    print(f"    scale-down: to {last} by {last_t:.0f} s" if last < peak else "    scale-down: not within the run")

    print("\nK8S TEST PASSED")


if __name__ == "__main__":
    main()
