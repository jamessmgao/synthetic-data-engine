"""Operate the AWS deployment from your machine (wrapped by `make aws-*`).

    bootstrap        create the Terraform state bucket (once)
    plan | apply     Terraform: VPC, RDS Postgres, ElastiCache Redis, S3, runner EC2
    secrets          copy OPENROUTER_API_KEY from .env into SSM (/sde/openrouter_api_key)
    deploy [REF]     ship git REF (default HEAD) to the runner and `compose up --build`
    smoke            run shared.jobs.smoke on the runner against RDS, Redis and S3
    status           what's running, and the deployed release
    logs [SERVICE]   last 100 log lines from the runner's containers
    exec CMD…        run a shell command on the runner
    stop | start     stop or start the runner (managed state keeps running)
    destroy          Terraform destroy: deletes everything it created

Every command checks that the AWS session is the expected account and region
first. Credentials come from your AWS CLI login (`aws login`), exported for
Terraform. Access to the runner is only through SSM: no SSH, no open ports.
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ACCOUNT = "284460774261"
REGION = "us-east-2"
REPO = Path(__file__).resolve().parents[1]
TF_DIR = REPO / "deploy" / "terraform"
STATE_BUCKET = f"sde-pipeline-tfstate-{ACCOUNT}"
TAGS = "TagSet=[{Key=project,Value=sde-task-pipeline},{Key=managed_by,Value=aws_ops}]"


def run(cmd: list[str], *, capture: bool = True, env: dict | None = None, check: bool = True,
        cwd: Path | None = None) -> str:
    p = subprocess.run(cmd, capture_output=capture, text=True, env=env, cwd=cwd)
    if check and p.returncode != 0:
        sys.exit(f"command failed ({p.returncode}): {' '.join(cmd[:4])} …\n{(p.stderr or '')[-2000:]}")
    return (p.stdout or "").strip()


def aws(*args: str, check: bool = True) -> str:
    return run(["aws", *args, "--region", REGION, "--output", "json"], check=check)


def awsj(*args: str):
    out = aws(*args)
    return json.loads(out) if out else None


def guard() -> None:
    ident = awsj("sts", "get-caller-identity")
    if ident["Account"] != ACCOUNT:
        sys.exit(f"refusing: logged in to account {ident['Account']}, expected {ACCOUNT}")


def tf_env() -> dict[str, str]:
    """Terraform can't read an `aws login` session directly: export it."""
    creds = json.loads(run(["aws", "configure", "export-credentials", "--format", "process"]))
    env = dict(os.environ)
    env.update(AWS_ACCESS_KEY_ID=creds["AccessKeyId"], AWS_SECRET_ACCESS_KEY=creds["SecretAccessKey"],
               AWS_REGION=REGION)
    if creds.get("SessionToken"):
        env["AWS_SESSION_TOKEN"] = creds["SessionToken"]
    env.pop("AWS_PROFILE", None)
    return env


def terraform(*args: str, capture: bool = False) -> str:
    env = tf_env()
    if not (TF_DIR / ".terraform").exists():
        run(["terraform", "init", "-input=false"], capture=False, env=env, cwd=TF_DIR)
    return run(["terraform", *args], capture=capture, env=env, cwd=TF_DIR)


def outputs() -> dict[str, str]:
    raw = json.loads(terraform("output", "-json", capture=True) or "{}")
    if not raw:
        sys.exit("no Terraform outputs: run `make aws-apply` first")
    return {k: v["value"] for k, v in raw.items()}


# --- runner commands over SSM ------------------------------------------------------


def wait_for_ssm(instance: str, timeout_s: int = 600) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        info = awsj("ssm", "describe-instance-information", "--filters",
                    f"Key=InstanceIds,Values={instance}")["InstanceInformationList"]
        if info and info[0]["PingStatus"] == "Online":
            return
        print("waiting for the runner to register with SSM…", flush=True)
        time.sleep(10)
    sys.exit(f"{instance} never came online in SSM")


def remote(instance: str, commands: list[str], timeout_s: int = 3600) -> str:
    """Run shell commands on the runner and return their output (fails on non-zero)."""
    wait_for_ssm(instance)
    params = json.dumps({"commands": commands, "executionTimeout": [str(timeout_s)]})
    cmd_id = awsj("ssm", "send-command", "--instance-ids", instance, "--document-name",
                  "AWS-RunShellScript", "--parameters", params, "--timeout-seconds", "60",
                  "--comment", "sde-task-pipeline")["Command"]["CommandId"]
    while True:
        time.sleep(3)
        inv = json.loads(aws("ssm", "get-command-invocation", "--command-id", cmd_id,
                             "--instance-id", instance, check=False) or "{}")
        status = inv.get("Status", "Pending")
        if status not in ("Pending", "InProgress", "Delayed"):
            break
    out = inv.get("StandardOutputContent", "") + inv.get("StandardErrorContent", "")
    if status != "Success":
        sys.exit(f"remote command {status}:\n{out[-6000:]}")
    return out


def remote_script(instance: str, script: Path, args: list[str]) -> str:
    b64 = base64.b64encode(script.read_bytes()).decode()
    return remote(instance, [
        f"echo {b64} | base64 -d > /tmp/{script.name}",
        f"bash /tmp/{script.name} {' '.join(args)}",
    ])


COMPOSE = ("cd /opt/sde/current && docker compose --env-file /opt/sde/runtime.env "
           "-f deploy/compose.prod.yaml")


# --- commands --------------------------------------------------------------------


def cmd_bootstrap() -> None:
    head = subprocess.run(["aws", "s3api", "head-bucket", "--bucket", STATE_BUCKET], capture_output=True)
    if head.returncode == 0:
        print(f"state bucket {STATE_BUCKET} already exists")
        return
    aws("s3api", "create-bucket", "--bucket", STATE_BUCKET,
        "--create-bucket-configuration", f"LocationConstraint={REGION}")
    aws("s3api", "put-bucket-versioning", "--bucket", STATE_BUCKET,
        "--versioning-configuration", "Status=Enabled")
    aws("s3api", "put-public-access-block", "--bucket", STATE_BUCKET, "--public-access-block-configuration",
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true")
    aws("s3api", "put-bucket-tagging", "--bucket", STATE_BUCKET, "--tagging", TAGS)
    print(f"created state bucket {STATE_BUCKET}")


def cmd_secrets() -> None:
    key = None
    candidates = [REPO / ".env"]
    common = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=REPO, check=False)
    if common:
        candidates.append(Path(common).parent / ".env")  # the main checkout, from a worktree
    for path in candidates:
        if path.exists():
            for line in path.read_text().splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
            if key:
                break
    if not key:
        sys.exit("OPENROUTER_API_KEY not found in .env")
    aws("ssm", "put-parameter", "--name", "/sde/openrouter_api_key", "--type", "SecureString",
        "--value", key, "--overwrite")
    aws("ssm", "add-tags-to-resource", "--resource-type", "Parameter", "--resource-id",
        "/sde/openrouter_api_key", "--tags", "Key=project,Value=sde-task-pipeline")
    print("stored /sde/openrouter_api_key (SecureString)")


def cmd_deploy(ref: str = "HEAD") -> None:
    out = outputs()
    sha = run(["git", "rev-parse", ref], cwd=REPO)
    key = f"releases/{sha}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "release.tar.gz"
        run(["git", "archive", "--format=tar.gz", "-o", str(tarball), sha], cwd=REPO)
        aws("s3", "cp", str(tarball), f"s3://{out['artifacts_bucket']}/{key}")
    print(f"uploaded {sha[:12]} → s3://{out['artifacts_bucket']}/{key}; deploying…", flush=True)
    print(remote_script(out["runner_instance_id"], REPO / "deploy" / "remote-deploy.sh",
                        [sha, out["artifacts_bucket"], REGION, str(out["docker_slots"])]))


def cmd_smoke() -> None:
    out = outputs()
    print(remote(out["runner_instance_id"],
                 [f"{COMPOSE} exec -T worker-control python -m shared.jobs.smoke"], timeout_s=300))


def cmd_status() -> None:
    out = outputs()
    inst = awsj("ec2", "describe-instances", "--instance-ids", out["runner_instance_id"],
                "--query", "Reservations[0].Instances[0].{state:State.Name,type:InstanceType}")
    db = awsj("rds", "describe-db-instances", "--db-instance-identifier", "sde-pipeline-db",
              "--query", "DBInstances[0].DBInstanceStatus")
    redis = awsj("elasticache", "describe-cache-clusters", "--cache-cluster-id", "sde-pipeline-redis",
                 "--query", "CacheClusters[0].CacheClusterStatus")
    print(json.dumps({"runner": inst, "rds": db, "redis": redis, **out}, indent=2))
    if inst["state"] == "running":
        print(remote(out["runner_instance_id"], [
            "readlink /opt/sde/current || echo 'no release deployed'",
            f"{COMPOSE} ps 2>/dev/null || true",
        ], timeout_s=120))


def cmd_logs(service: str = "") -> None:
    out = outputs()
    print(remote(out["runner_instance_id"], [f"{COMPOSE} logs --no-color --tail 100 {service}"],
                 timeout_s=120))


def cmd_exec(*command: str) -> None:
    out = outputs()
    print(remote(out["runner_instance_id"], [" ".join(command)], timeout_s=600))


def cmd_stop() -> None:
    aws("ec2", "stop-instances", "--instance-ids", outputs()["runner_instance_id"])
    print("runner stopping (RDS, Redis and S3 keep running; `make aws-destroy` removes them)")


def cmd_start() -> None:
    inst = outputs()["runner_instance_id"]
    aws("ec2", "start-instances", "--instance-ids", inst)
    wait_for_ssm(inst)
    print("runner online; the containers restart on their own (restart: unless-stopped)")


COMMANDS = {
    "bootstrap": cmd_bootstrap,
    "plan": lambda: terraform("plan", "-input=false"),
    "apply": lambda: terraform("apply", "-input=false", "-auto-approve"),
    "destroy": lambda: terraform("destroy", "-input=false"),
    "secrets": cmd_secrets,
    "deploy": cmd_deploy,
    "smoke": cmd_smoke,
    "status": cmd_status,
    "logs": cmd_logs,
    "exec": cmd_exec,
    "stop": cmd_stop,
    "start": cmd_start,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    guard()
    COMMANDS[sys.argv[1]](*sys.argv[2:])


if __name__ == "__main__":
    main()
