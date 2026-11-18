# Hill-climbing goal

Run the hill-climbing loop on the production K3s cluster (AWS) exactly as docs/HILLCLIMBING_ALGORITHM.md describes (with
docs/ITERATION_RUNBOOK.md): a sliding window of continuous tasks, run as six lanes (one per agent session, each with ~1/6 of
the cluster's capacity, its own strategy and lever, watching its own tasks and proposing tweaks on its own branch); you are the
manager: you merge the lanes' tweaks into main and deploy them periodically with drains as their mini-batches complete; each
change is judged on the tasks authored after it and logged in docs/ITERATION_LOG.md. Goal: novel, diverse tasks the pinned
solver (terminus-2 + glm-5.3-flash) passes 1-3 out of 5 times; optimize task generation and the revision pipeline. Branch: main
only. Hard limits: Terraform or node/pod-count changes only with my explicit OK; never aws-k8s-down, delete RDS/S3 data,
change security groups or toggle mock mode; never set a short solver timeout. Done when at least 80% of the tasks in a window
of 10+ are ACCEPTED in band because they are genuinely difficult; record that evidence in the log.
