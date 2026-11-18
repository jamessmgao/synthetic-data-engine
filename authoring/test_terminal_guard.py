"""authoring/profile.d/10-sde-terminal.sh: in the agent's interactive terminal, `exit` and a
later-tripping `set -e` can't end the session; scripts and non-interactive shells are
unaffected."""

import subprocess
from pathlib import Path

GUARD = Path(__file__).resolve().parent / "profile.d" / "10-sde-terminal.sh"


def terminal(lines: list[str]) -> subprocess.CompletedProcess:
    """An interactive bash fed line by line on stdin, as terminus-2 types into tmux: bash
    shows a prompt (and runs PROMPT_COMMAND) between lines."""
    script = "\n".join([f"source {GUARD}", *lines, "echo END-OF-SESSION"]) + "\n"
    return subprocess.run(["bash", "--norc", "--noprofile", "-i"], input=script,
                          capture_output=True, text=True, timeout=30)


def test_exit_does_not_end_the_session():
    out = terminal(["exit 3", 'echo "alive rc=$?"', "logout", "echo alive2"])
    assert "alive rc=3" in out.stdout and "alive2" in out.stdout and "END-OF-SESSION" in out.stdout
    assert "'exit' is disabled" in out.stderr


def test_exit_in_a_subshell_still_exits_the_subshell():
    out = terminal(['( if true; then exit 4; fi; echo NOT-REACHED ); echo "sub rc=$?"',
                    'x=$( exit 5 ); echo "cmdsub rc=$?"'])
    assert "sub rc=4" in out.stdout and "cmdsub rc=5" in out.stdout and "NOT-REACHED" not in out.stdout
    assert "END-OF-SESSION" in out.stdout


def test_set_e_at_the_prompt_cannot_end_the_session_on_a_later_failure():
    for form in ("set -e", "set -eu", "set -euo pipefail", "set -o errexit"):
        out = terminal([form, "false", "echo survived"])
        assert "survived" in out.stdout and "END-OF-SESSION" in out.stdout, form


def test_set_still_works_for_everything_else():
    out = terminal(["set -- a b", 'echo "second=$2"', "set -o pipefail", 'echo "opts=$-"'])
    assert "second=b" in out.stdout and "END-OF-SESSION" in out.stdout


def test_scripts_and_non_interactive_shells_are_unaffected():
    out = subprocess.run(["bash", "-c", f"source {GUARD}; set -e; false; echo NOT-REACHED"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 1 and "NOT-REACHED" not in out.stdout
    out = subprocess.run(["bash", "-c", f"source {GUARD}; exit 7"], capture_output=True, text=True)
    assert out.returncode == 7
