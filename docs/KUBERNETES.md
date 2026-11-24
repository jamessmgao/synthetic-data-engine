# Kubernetes (K3s on AWS, k3d locally)

An index. The cluster as built is described in the deployment doc; the plans it was built from
are archived.

**As built**
- [DEPLOY.md](DEPLOY.md) §8: what runs where (§8.1), the Docker sidecar per worker pod (§8.2),
  KEDA autoscaling (§8.3), the commands and the chaos test (§8.4), local vs AWS (§8.5), and K3s
  on the AWS EC2 instances with its overlays (§8.6).
- [CAPACITY.md](CAPACITY.md): the cluster's size, concurrency limits and cost.
- The manifests are in `infra/k8s/` (`base/` and `overlays/`); the scripts are
  `scripts/k8s-local.sh`, `scripts/k8s_aws.py` and `scripts/k8s_test.py`.

**The plans (historical)**
- [archive/K8S_PLAN.md](archive/K8S_PLAN.md): the pipeline on a local K3s cluster: the design
  (§2), how worker pods get Docker (§3), and the same cluster on the AWS runners (§10).
- [archive/K3S_PLAN.md](archive/K3S_PLAN.md): the first plan for K3s on local k3d and AWS, with
  autoscaling (§4).
