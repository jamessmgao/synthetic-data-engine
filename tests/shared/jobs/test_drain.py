"""Drain on deploy (shared/jobs/worker.py Shutdown): with a drain, SIGTERM stops claiming and lets
the running job finish and settle; at the deadline, or on a second signal, what still runs is
handed back. Without one, SIGTERM hands back at once (the old behaviour)."""
import threading
import time

from shared.jobs import worker


class Queue:
    def __init__(self):
        self.jobs = [{"id": "j1", "lease_token": 1, "type": "t"}]
        self.claims, self.settled, self.released = 0, [], []

    def claim(self, *a, **k):
        self.claims += 1
        return self.jobs.pop() if self.jobs else None

    def heartbeat(self, *a, **k):
        return True

    def settle(self, job_id, token, fields, then=None):
        if job_id in self.released:
            return False  # fenced: the job was handed back
        self.settled.append(job_id)
        return True

    def release(self, job_id, token, **k):
        self.released.append(job_id)
        return True


def run(drain_s, job_s, signals=1, wait_s=0.1):
    q, stop = Queue(), threading.Event()
    shutdown = worker.Shutdown(stop, drain_s, interrupted=lambda: False)
    started = threading.Event()

    def job(_):
        started.set()
        time.sleep(job_s)
        return {"outcome": "OK"}

    t = threading.Thread(target=worker.serve, args=(q, "t", job), kwargs={"stop": stop})
    t.start()
    assert started.wait(2)
    claims_at_signal = q.claims
    for _ in range(signals):
        shutdown()
    time.sleep(wait_s)
    t.join(5)
    shutdown.finished.set()
    return q, claims_at_signal


def test_drain_lets_the_running_job_finish():
    q, claims = run(drain_s=5, job_s=0.3)
    assert q.settled == ["j1"] and q.released == []
    assert q.claims == claims  # nothing claimed after the signal


def test_drain_deadline_hands_the_job_back():
    q, _ = run(drain_s=0.1, job_s=0.6, wait_s=0.3)
    assert q.released == ["j1"] and q.settled == []


def test_second_signal_hands_back_at_once():
    q, _ = run(drain_s=60, job_s=0.4, signals=2)
    assert q.released == ["j1"] and q.settled == []


def test_no_drain_hands_back_at_once():
    q, _ = run(drain_s=0, job_s=0.4)
    assert q.released == ["j1"] and q.settled == []


# --- a Spot interruption: hand back at once, whatever the drain ---------------------

def run_spot(interrupted, job_s=0.6, drain_s=60, wait_s=0.3):
    q, stop = Queue(), threading.Event()
    shutdown = worker.Shutdown(stop, drain_s, interrupted=interrupted)
    started = threading.Event()

    def job(_):
        started.set()
        time.sleep(job_s)
        return {"outcome": "OK"}

    t = threading.Thread(target=worker.serve, args=(q, "t", job), kwargs={"stop": stop})
    t.start()
    assert started.wait(2)
    shutdown()
    time.sleep(wait_s)
    t.join(5)
    shutdown.finished.set()
    return q


def test_spot_interruption_at_sigterm_hands_back_at_once():
    q = run_spot(lambda: True)
    assert q.released == ["j1"] and q.settled == []


def test_spot_notice_during_a_drain_hands_back(monkeypatch):
    monkeypatch.setattr(worker, "SPOT_CHECK_S", 0.05)
    calls = {"n": 0}

    def interrupted():  # no notice at SIGTERM; one two checks later
        calls["n"] += 1
        return calls["n"] >= 3
    q = run_spot(interrupted, job_s=1.0, wait_s=0.5)
    assert q.released == ["j1"] and q.settled == []


def test_no_spot_notice_drains_normally():
    q = run_spot(lambda: False, job_s=0.3)
    assert q.settled == ["j1"] and q.released == []


def test_imds_check_is_false_off_ec2(monkeypatch):
    monkeypatch.setattr(worker, "IMDS", "http://127.0.0.1:9")  # nothing listens there
    assert worker.spot_interruption_pending(timeout_s=0.2) is False
