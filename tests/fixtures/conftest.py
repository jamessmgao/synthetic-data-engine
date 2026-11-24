"""Fixture data, not tests: p1-local-1/task/tests/test_outputs.py is the task's own hidden
test file (it runs inside the task's container), so pytest must not collect below here."""

collect_ignore_glob = ["*"]
