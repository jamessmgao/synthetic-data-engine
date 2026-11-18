# The authoring agent's terminal is an interactive login shell inside tmux (terminus-2).
# `exit` at its prompt, or a `set -e` that later trips, ends that shell, which ends the tmux
# session, and the trial dies with "no server running on /tmp/tmux-0/default" or
# "Session has ended". Batch aws-agent-b1 lost a world (25 min of work) and a retry this way:
# the agent ended commands with `; exit $rc`, and ran `set -eu` at the prompt.
# Interactive shells only: scripts, `bash -c '…'` and heredocs run in new shells, where
# exit and set -e behave normally.
case $- in
  *i*)
    # `exit` / `logout` at the prompt warn and return instead of ending the session.
    exit() {
      # In a subshell ( … ), $(…) or a pipeline, exit only leaves that subshell: let it.
      if (( BASH_SUBSHELL > 0 )); then builtin exit "$@"; fi
      echo "[sde] 'exit' is disabled in this terminal (it would end the session). Use a" \
           "subshell: ( …; exit \$rc ) or bash -c '…'. Returning ${1:-0} instead." >&2
      return "${1:-0}"
    }
    logout() { exit "$@"; }
    # errexit switched on at the prompt is switched off again before the next prompt, so a
    # later failing command can't end the session. (A `set` wrapper function can't do this:
    # `set -- …` inside a function sets the function's arguments, not the shell's.)
    PROMPT_COMMAND="builtin set +e${PROMPT_COMMAND:+; $PROMPT_COMMAND}"
    # The continuation prompt says what the shell is waiting for. A heredoc closed with the
    # wrong delimiter (`</EOF>` for EOF, aws-w1-l3-t01's world) leaves the shell reading the
    # heredoc, every later command becomes file content, and the plain "> " went unnoticed for
    # 23 minutes. Short, as it prefixes every line of a legitimate multi-line command too.
    PS2='[sde: heredoc or quote still open; end it with its delimiter alone on a line, or C-c]> '
    ;;
esac
