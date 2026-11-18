"""The world scaffolds: deterministic, replayable, stdlib only."""

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from sde_fault import Crash, Faults  # noqa: E402
from sde_repro import check, repro_main, run_repro  # noqa: E402
from sde_sched import ScheduleError, Scheduler, explore, load_schedule, save_schedule  # noqa: E402


# --- a tiny world: two clients doing read-modify-write on one counter -----------------


class Store:
    def __init__(self):
        self.x = 0


def increment(store, locked=False):
    if locked:  # the fix: read and write in one step
        store.x += 1
        yield "read+write"
        return
    v = store.x
    yield "read"
    store.x = v + 1
    yield "write"


def build(locked=False):
    def b(s):
        store = Store()
        s.spawn("a", increment(store, locked))
        s.spawn("b", increment(store, locked))
        return store
    return b


def lost_update(store):
    return None if store.x == 2 else f"x == {store.x}, expected 2"


# --- the scheduler -------------------------------------------------------------------


def test_a_seed_gives_the_same_interleaving_every_time():
    runs = []
    for _ in range(3):
        s = Scheduler(seed=11)
        build()(s)
        runs.append(s.run())
    assert runs[0] == runs[1] == runs[2]


def test_explore_finds_the_lost_update_and_replay_reproduces_it(tmp_path):
    found = explore(build(), lost_update, seeds=range(50))
    assert found is not None and found["violation"] == "x == 1, expected 2"
    save_schedule(tmp_path / "s.json", found["schedule"])
    for _ in range(3):  # replay: the same violation, step for step
        s = Scheduler(replay=load_schedule(tmp_path / "s.json"))
        store = build()(s)
        assert s.run() == found["trace"] and lost_update(store) == "x == 1, expected 2"


def test_explore_returns_none_when_every_seed_holds():
    assert explore(build(locked=True), lost_update, seeds=range(50)) is None


def test_replaying_an_unrunnable_actor_is_an_error():
    s = Scheduler(replay=["a", "a", "a", "a"])
    build()(s)
    with pytest.raises(ScheduleError):
        s.run()


def test_actor_results_and_seed_xor_replay():
    def actor():
        yield "one"
        return 42
    s = Scheduler(seed=0)
    s.spawn("a", actor())
    s.run()
    assert s.results == {"a": 42}
    with pytest.raises(ValueError):
        Scheduler()
    with pytest.raises(ValueError):
        Scheduler(seed=1, replay=[])


# --- faults --------------------------------------------------------------------------


def test_crash_on_the_planned_hit_only():
    f = Faults({"crash": ["after_commit#2"]})
    f.point("after_commit")
    with pytest.raises(Crash) as e:
        f.point("after_commit")
    assert (e.value.point, e.value.hit) == ("after_commit", 2)
    f.point("after_commit")
    assert f.fired == ["crash after_commit#2"]


def test_drop_and_duplicate_by_message_number():
    f = Faults({"drop": {"ack": [2]}, "duplicate": {"ack": [3]}})
    assert [f.deliver("ack", i) for i in (1, 2, 3, 4)] == [[1], [], [3, 3], [4]]
    assert f.deliver("vote", "v") == ["v"]
    assert f.fired == ["drop ack#2", "duplicate ack#3"]


def test_a_partial_write_tears_then_crashes():
    f, buf = Faults({"partial": {"wal": 4}}), io.StringIO()
    with pytest.raises(Crash):
        f.write(buf, "record-1\n", at="wal")
    assert buf.getvalue() == "reco"
    f.write(buf, "record-2\n", at="wal")  # once only
    assert buf.getvalue() == "recorecord-2\n"


def test_plan_from_env_inline_or_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SDE_FAULTS", '{"crash": ["p"]}')
    with pytest.raises(Crash):
        Faults.from_env().point("p")
    (tmp_path / "plan.json").write_text('{"drop": {"m": [1]}}')
    monkeypatch.setenv("SDE_FAULTS", str(tmp_path / "plan.json"))
    assert Faults.from_env().deliver("m", 1) == []
    monkeypatch.delenv("SDE_FAULTS")
    Faults.from_env().point("p")  # no plan, no faults
    with pytest.raises(ValueError):
        Faults({"crsh": []})


# --- the repro harness ---------------------------------------------------------------


INVARIANTS = {"no lost update": lambda store: lost_update(store) or True}


def replay(schedule, locked=False):
    s = Scheduler(replay=schedule)
    store = build(locked)(s)
    s.run()
    return store


def test_repro_exit_codes(capsys):
    bad = explore(build(), lost_update, seeds=range(50))["schedule"]
    assert run_repro(bad, replay, INVARIANTS) == 1
    assert "VIOLATED: no lost update: x == 1" in capsys.readouterr().out
    fixed = lambda seq: replay(["a", "a", "b", "b"], locked=True)  # noqa: E731
    assert run_repro(bad, fixed, INVARIANTS) == 0
    assert run_repro(bad, lambda seq: 1 / 0, INVARIANTS) == 2  # a broken harness is not "fixed"
    assert check(Store(), {"holds": lambda s: True, "fails": lambda s: False}) == ["fails"]


def test_repro_main_reads_the_sequence_file(tmp_path):
    (tmp_path / "seq.json").write_text(json.dumps(explore(build(), lost_update, seeds=range(50))["schedule"]))
    with pytest.raises(SystemExit) as e:
        repro_main(replay, INVARIANTS, argv=["--sequence", str(tmp_path / "seq.json")])
    assert e.value.code == 1
    with pytest.raises(SystemExit) as e:
        repro_main(replay, INVARIANTS, argv=["--sequence", str(tmp_path / "missing.json")])
    assert e.value.code == 2


@pytest.mark.skipif(subprocess.run(["which", "cc"], capture_output=True).returncode != 0, reason="no C compiler")
def test_the_readmes_c_crash_point_compiles_and_crashes_on_the_planned_hit(tmp_path):
    readme = (HERE / "README.md").read_text()
    snippet = readme.split("```c\n", 1)[1].split("```", 1)[0]
    (tmp_path / "w.c").write_text(
        "#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n#include <unistd.h>\n" + snippet +
        'int main(void) { for (int i = 1; i <= 3; i++) { printf("commit %d\\n", i); fflush(stdout);'
        ' FAULT_POINT("after_commit"); } return 0; }\n')
    subprocess.run(["cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-Wall", "-Werror", "-o",
                    str(tmp_path / "w"), str(tmp_path / "w.c")], check=True)
    run = lambda plan: subprocess.run([str(tmp_path / "w")], capture_output=True, text=True,  # noqa: E731
                                      env={"SDE_CRASH": plan} if plan else {})
    assert run(None).returncode == 0
    p = run("after_commit#2")
    assert p.returncode == 137 and p.stdout == "commit 1\ncommit 2\n"
    assert run("after_commit").stdout == "commit 1\n"


def test_stdlib_only():
    """Vendored into a world, they must import on a bare python3 (no site-packages)."""
    code = f"import sys; sys.path.insert(0, {str(HERE)!r}); import sde_sched, sde_fault, sde_repro"
    subprocess.run([sys.executable, "-I", "-S", "-c", code], check=True)
