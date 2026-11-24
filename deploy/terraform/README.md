# Terraform for the AWS stack

**Do not apply this configuration against the stack that is already deployed.**

On 2026-11-24 every project-prefixed name in this repo was changed to the `sde` prefix. The
change was made in the files only. Nothing was run against AWS, so the deployed stack and its
Terraform state still use the earlier prefix. Until the stack is migrated or rebuilt:

- `terraform apply` from this directory against the existing state would plan to destroy and
  recreate the renamed resources, including the RDS database and the artifacts bucket.
- The `make aws-*` and `make aws-k8s-*` targets in this checkout look for the `sde` names, so
  they do not work against the deployed cluster. Operate it from a copy of the repo that
  predates the rename.

Use this configuration as it is only for a new stack with a new state bucket.

## Names that differ from the deployed stack

Each of these has the `sde` prefix here and the earlier prefix in the deployed stack.

| What | In the repo now |
|---|---|
| Resource name prefix (`var.name`) | `sde-pipeline` |
| Project tag (`var.project`) | `sde-task-pipeline` |
| Artifacts bucket | `sde-pipeline-artifacts-<account id>` |
| Terraform state bucket and key | `sde-pipeline-tfstate-<account id>`, `sde-task-pipeline/terraform.tfstate` |
| RDS database name and user | `sde` |
| SSM parameters, and the instances' IAM read scope | `/sde/*` |
| Directory on the instances | `/opt/sde` |
| Kubernetes namespace | `sde` |
| ConfigMap and Secret | `sde-config`, `sde-secrets` |
| Images | `sde-pipeline`, `sde-authoring`, `sde-dashboard` |
| Node labels and taint | `sde/lifecycle`, `sde/role`, `sde/joining` |
| Redis key prefix | `sde:` |
| Settings in the deployed env files | `SDE_*` |
| Label on job containers | `sde.job` |

One parameter is new and has no deployed counterpart: the gateway base URL,
`/sde/openrouter_api_base`, written by `make aws-secrets` from `OPENROUTER_API_BASE` in `.env`.
