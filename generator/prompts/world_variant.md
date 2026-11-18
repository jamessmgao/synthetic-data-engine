You are making a VARIANT of an existing world: the same codebase and domain, with its
defect replaced by a new one. No agent has run on it yet: the new task is a new problem
in the same code, so it must need a different fix.

# The variant to make

1. **Remove the existing defect:** apply the parent's fix to the code, so that bug is gone.
2. Plant ONE new defect of mechanism **$mechanism**, in a different function, component
   or interaction, reached by a different sequence of operations.

   $mechanism_line

3. **Write everything new for it** in `world.json` and the scripts: the invariants it
   breaks (the new ones only), the counterexample (`repro/repro.sh`,
   `repro/symptom_check.sh`), `world_fix/fix.sh`, and a plausible wrong fix
   (`world_fix/wrong_fix.sh`: it clears the visible symptom and still fails the repro).
4. **Check it's a different defect:** the parent's own fix
   (`$parent_dir/world_fix/fix.sh`) must NOT make your world correct. Write that as a check
   and run it.

Keep the size: change how existing modules behave, don't add modules. Not a variant: the
old bug renamed, moved a few lines, or still reachable. Where the sections below speak of
"the strategy that passed" or `target_fix`, read: the parent's fix.

Tasks already written on this world (the new defect and its symptom must differ from all
of them):

$earlier_tasks
