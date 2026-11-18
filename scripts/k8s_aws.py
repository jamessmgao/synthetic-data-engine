"""The pipeline on K3s across the AWS runners (wrapped by `make aws-k8s-*`; docs/K8S_PLAN.md).

    up        stop compose on every runner; K3s server on runners()[0], agents on the rest;
              build + import sde-pipeline:local on every node (crictl-checked); install KEDA
    deploy    ship REF (default HEAD), rebuild + import the image, apply infra/k8s/overlays/aws,
              wait for the rollouts. `--overlay aws-prod`: the real stores and model from SSM
              (Secret sde-secrets), the authoring image on every node, and an IMDS check
    status    nodes, pods, services
    logs SVC  last 100 lines: api|control|validate|executor|generation|postgres|redis|s3|bootstrap
    test ARGS run scripts/k8s_test.py ARGS on the server, from the last shipped release, as the
              transient systemd unit sde-k8s-test (an SSM timeout would kill a nohup'd one)
    test-log  that unit's journal
    down      uninstall K3s on every node; then `make aws-deploy` restores compose

Everything runs over SSM through deploy/aws_ops.py (same account and region guard). The K3s
join token lives in the SSM SecureString /sde/k3s_token, generated once; nodes read it
themselves, so it never appears in a command, its output or this machine's logs.
"""

import os
import secrets
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import aws_ops  # noqa: E402
from aws_ops import COMPOSE, REGION, REPO, aws, on_all, outputs, remote, run, runners  # noqa: E402

K3S_VERSION = "v1.35.5+k3s1"
KEDA_URL = "https://github.com/kedacore/keda/releases/download/v2.16.1/keda-2.16.1.yaml"
TOKEN_PARAM = "/sde/k3s_token"
IMAGE = "sde-pipeline:local"
# aws-prod-mock: production stores and glue, but mock models and no authoring claims.
OVERLAYS = ("aws", "aws-prod", "aws-prod-mock")
NS = "sde"
KUBECTL = "k3s kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml"
K = f"{KUBECTL} -n {NS}"
CURRENT = "/opt/sde/k8s-current"  # the release last shipped to the cluster (compose keeps /opt/sde/current)
TEST_UNIT = "sde-k8s-test"

# Read on the node, never passed in: the token stays out of SSM command text and output.
TOKEN = (f"$(aws ssm get-parameter --region {REGION} --name {TOKEN_PARAM} --with-decryption "
         "--query Parameter.Value --output text)")


# aws-prod's Secret sde-secrets: key → SSM parameter (or a literal), read on the server.
PROD_SECRETS = {
    "DATABASE_URL": "/sde/database_url",
    "REDIS_URL": "/sde/redis_url",
    "S3_BUCKET": "/sde/s3_bucket",
    "OPENROUTER_API_KEY": "/sde/openrouter_api_key",
    "OPENROUTER_API_BASE": "https://gateway.example.com/openrouter/api/v1",
    "SDE_API_KEY": "/sde/api_key",
    "SDE_BUDGET_USD": "/sde/budget_usd",
}
PROD_DEFAULTS = {"SDE_BUDGET_USD": "40"}  # as remote-deploy.sh's param_or


def ensure_token() -> None:
    have = subprocess.run(["aws", "ssm", "get-parameter", "--region", REGION, "--name", TOKEN_PARAM],
                          capture_output=True).returncode == 0
    if have:
        return
    # From a 0600 temp file, so the value isn't in this machine's process list either.
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write(secrets.token_hex(32))
    try:
        aws("ssm", "put-parameter", "--name", TOKEN_PARAM, "--type", "SecureString",
            "--value", f"file://{f.name}")
    finally:
        os.unlink(f.name)
    aws("ssm", "add-tags-to-resource", "--resource-type", "Parameter", "--resource-id", TOKEN_PARAM,
        "--tags", "Key=project,Value=sde-task-pipeline")
    print(f"generated {TOKEN_PARAM} (SecureString)")


def ship(ref: str) -> str:
    """Upload git REF as a release (as `aws_ops deploy` does) and unpack it on every runner."""
    out = outputs()
    sha = run(["git", "rev-parse", ref], cwd=REPO)
    key = f"releases/{sha}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "release.tar.gz"
        run(["git", "archive", "--format=tar.gz", "-o", str(tarball), sha], cwd=REPO)
        aws("s3", "cp", str(tarball), f"s3://{out['artifacts_bucket']}/{key}")
    release = f"/opt/sde/releases/{sha}"
    unpack = [
        "until [ -f /opt/sde/.bootstrapped ]; do sleep 5; done",
        f"if [ ! -d {release} ]; then tmp=$(mktemp -d /opt/sde/releases/.{sha}.XXXX)"
        f" && aws s3 cp --region {REGION} s3://{out['artifacts_bucket']}/{key} - | tar -xz -C $tmp"
        f" && mv $tmp {release}; fi",
        f"ln -sfn {release} {CURRENT}",
    ]
    on_all(lambda i: remote(i, unpack, timeout_s=600), runners())
    print(f"shipped {sha[:12]} to every runner")
    return release


def build_and_import(release: str) -> None:
    """Build the image into each node's Docker and copy it into K3s's containerd.

    `ctr import` can report success without the image landing, so crictl checks each node.
    """
    cmds = [
        f"cd {release} && docker build -q --provenance=false --sbom=false"
        f" -f deploy/Dockerfile --target runner -t {IMAGE} . >/dev/null",
        f"docker save {IMAGE} | k3s ctr -n k8s.io images import - >/dev/null",
        f"k3s crictl images -q docker.io/library/{IMAGE} | grep -q ."
        f" || {{ echo \"image {IMAGE} missing on $(hostname) after import\" >&2; exit 1; }}",
        f"echo \"$(hostname): {IMAGE} imported\"",
    ]
    for log in on_all(lambda i: remote(i, cmds, timeout_s=1800), runners()):
        print(log.strip())


def wait_ready(server: str, agents: list[str], want: int) -> None:
    """Wait up to 5 min for `want` Ready nodes; on timeout, show each missing agent's K3s log."""
    nodes = remote(server, [
        f"for _ in $(seq 100); do [ $({KUBECTL} get nodes --no-headers | awk '$2 == \"Ready\"' | wc -l)"
        f" -ge {want} ] && break; sleep 3; done",
        f"{KUBECTL} get nodes --no-headers",
    ], timeout_s=400)
    ready = {line.split()[0] for line in nodes.splitlines() if line.split()[1:2] == ["Ready"]}
    if len(ready) >= want:
        return
    print(f"{len(ready)} of {want} nodes Ready:\n{nodes}", flush=True)
    for inst, host in zip(agents, on_all(lambda i: remote(i, ["hostname"], timeout_s=60).strip(), agents)):
        if host not in ready:
            log = remote(inst, ["journalctl -u k3s-agent -n 20 --no-pager || true"], timeout_s=60)
            print(f"=== {inst} ({host}): not Ready ===\n{log}", flush=True)
    sys.exit(f"{len(ready)} of {want} nodes Ready")


def cmd_up(ref: str = "HEAD") -> None:
    ids = runners()
    server, agents = ids[0], ids[1:]
    ensure_token()
    print("stopping compose on every runner…", flush=True)
    on_all(lambda i: remote(i, [f"[ -e /opt/sde/current ] && {COMPOSE} down || true"], timeout_s=600), ids)

    server_ip = remote(server, ["hostname -I | awk '{print $1}'"], timeout_s=60).strip()
    install = f"curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION={K3S_VERSION} K3S_TOKEN={TOKEN}"
    print(f"K3s server on {server} ({server_ip})…", flush=True)
    remote(server, [
        f"systemctl is-active --quiet k3s || {{ {install} sh -s - server"
        # The VPC is 10.42.0.0/16, K3s's default pod CIDR: move pods and services off it.
        f" --node-ip {server_ip} --cluster-cidr=10.52.0.0/16 --service-cidr=10.53.0.0/16"
        " --write-kubeconfig-mode 600 >/dev/null; }",
        f"until {KUBECTL} get nodes >/dev/null 2>&1; do sleep 3; done",
    ], timeout_s=900)
    print(f"K3s agents on {len(agents)} runners…", flush=True)
    try:
        on_all(lambda i: remote(i, [
            "systemctl is-active --quiet k3s-agent || {"
            f" ip=$(hostname -I | awk '{{print $1}}');"
            f" {install} K3S_URL=https://{server_ip}:6443 sh -s - agent --node-ip $ip >/dev/null; }}",
        ], timeout_s=900), agents)
    except SystemExit as e:  # still report which agents aren't Ready, with their logs
        print(f"an agent install failed: {e}", flush=True)
    wait_ready(server, agents, len(ids))

    build_and_import(ship(ref))
    print("installing KEDA…", flush=True)
    print(remote(server, [
        f"{KUBECTL} apply --server-side --force-conflicts -f {KEDA_URL} >/dev/null",
        f"{KUBECTL} -n keda rollout status deploy/keda-operator --timeout=300s",
    ], timeout_s=400))
    cmd_status()


def authoring_image(release: str) -> str:
    """Build the authoring image on every node (as remote-deploy.sh does) and save it to
    /opt/sde/images/sde-authoring.tar, where the dind sidecars load it. Returns its tag."""
    tar = "/opt/sde/images/sde-authoring.tar"
    cmds = [
        f"cd {release}",
        # Tagged by the content of what it bakes in: a change rebuilds, an unchanged one is reused.
        "tag=$(find authoring config generator validator -type f ! -path '*/__pycache__/*' -print0"
        " | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12)",
        "img=sde-authoring:$tag",
        "docker image inspect $img >/dev/null 2>&1"
        " || docker build -q -f authoring/Dockerfile -t $img . >/dev/null",
        "mkdir -p /opt/sde/images",
        f'[ "$(cat {tar}.tag 2>/dev/null)" = "$img" ]'
        f" || {{ docker save -o {tar}.tmp $img && mv {tar}.tmp {tar} && echo $img > {tar}.tag; }}",
        "echo $img",
    ]
    tags = {log.strip().splitlines()[-1] for log in on_all(lambda i: remote(i, cmds, timeout_s=1800), runners())}
    if len(tags) != 1:
        sys.exit(f"the nodes disagree on the authoring image: {sorted(tags)}")
    tag = tags.pop()
    print(f"{tag} saved to {tar} on every node")
    return tag


def prod_secret_cmds() -> list[str]:
    """Create or refresh Secret sde-secrets from SSM. The values exist only on the server, in a
    0600 temp file: never in the command text, its output, or kubectl's arguments."""
    get = f"aws ssm get-parameter --region {REGION} --with-decryption --query Parameter.Value --output text"
    lines = []
    for key, src in PROD_SECRETS.items():
        if not src.startswith("/"):
            lines.append(f"echo {key}={shlex.quote(src)}")
        elif key in PROD_DEFAULTS:
            lines.append(f"echo {key}=$({get} --name {src} 2>/dev/null || echo {PROD_DEFAULTS[key]})")
        else:
            lines.append(f"echo {key}=$({get} --name {src})")
    return [
        "f=$(umask 077; mktemp)",
        "{ " + "; ".join(lines) + "; } > $f",
        # A missing parameter leaves an empty value: name the keys, never the values.
        "if grep -q '=$' $f; then echo \"SSM parameter missing for: $(grep '=$' $f | cut -d= -f1 | xargs)\" >&2;"
        " rm -f $f; exit 1; fi",
        f"{K} create secret generic sde-secrets --from-env-file=$f --dry-run=client -o yaml"
        f" | {KUBECTL} apply -f - >/dev/null; rc=$?; rm -f $f; [ $rc -eq 0 ]",
        "echo 'Secret sde-secrets refreshed from SSM'",
    ]


def cmd_deploy(ref: str = "HEAD", overlay: str = "aws") -> None:
    if overlay not in OVERLAYS:
        sys.exit(f"--overlay must be one of {', '.join(OVERLAYS)}")
    prod = overlay.startswith("aws-prod")
    path = f"infra/k8s/overlays/{overlay}"
    release = ship(ref)
    build_and_import(release)
    pre = [f"cd {release} && touch deploy/local.env"]  # the base's optional settings file
    if prod:
        tag = authoring_image(release)
        # The overlay merges this into sde-config (agreed with K1); the file is gitignored.
        # aws-prod's configMapGenerator reads it, also when aws-prod-mock builds on aws-prod.
        pre += [f"test -d {path} || {{ echo 'no {path} in this release' >&2; exit 1; }}",
                f"echo SDE_AUTHORING_IMAGE={tag} > infra/k8s/overlays/aws-prod/authoring.env",
                *prod_secret_cmds()]
    print(f"deploying {path}…", flush=True)
    print(remote(runners()[0], [
        *pre,
        # Jobs are immutable: replace the bootstrap job so it runs again on every deploy.
        f"{K} delete job bootstrap --ignore-not-found",
        f"{KUBECTL} kustomize --load-restrictor LoadRestrictionsNone {path} | {KUBECTL} apply -f -",
        # Same tag, new image contents: restart so the pods pick up what was just imported.
        f"{K} rollout restart deploy",
        f"for s in $({K} get statefulset -o name); do {K} rollout status $s --timeout=300s; done",
        f"if {K} get job/bootstrap >/dev/null 2>&1; then"
        f" {K} wait job/bootstrap --for=condition=complete --timeout=300s; fi",
        f"for d in $({K} get deploy -o name); do {K} rollout status $d --timeout=300s; done",
    ], timeout_s=1800))
    cmd_status()
    if prod:
        # Pods reach S3 with the instance role through IMDS (needs a hop limit of 2).
        print("IMDS from a pod:", remote(runners()[0], [
            f"{K} exec deploy/executor -c app -- python -c \"import boto3;"
            " print(boto3.client('sts').get_caller_identity()['Arn'])\" 2>&1 | tail -3"
            " || echo 'IMDS check failed'"], timeout_s=120).strip())


def cmd_status() -> None:
    print(remote(runners()[0], [f"{KUBECTL} get nodes -o wide", "echo", f"{K} get pods -o wide",
                                "echo", f"{K} get svc"], timeout_s=120))


def cmd_logs(svc: str = "executor") -> None:
    print(remote(runners()[0], [f"{K} logs -l app.kubernetes.io/name={shlex.quote(svc)}"
                                " --all-containers --prefix --tail=100"], timeout_s=120))


def cmd_public() -> None:
    """The public HTTPS link of the API (the aws-prod overlays' Cloudflare quick tunnel)."""
    out = remote(runners()[0], [f"{K} logs deploy/public-tunnel --tail=200 2>&1"
                                " | grep -o 'https://[a-z0-9-]*\\.trycloudflare\\.com' | tail -1"], timeout_s=120)
    print(out.strip() or "no tunnel URL yet (is deploy/public-tunnel running?)")


def cmd_test(*args: str) -> None:
    argv = " ".join(shlex.quote(a) for a in
                    ["--kubeconfig", "/etc/rancher/k3s/k3s.yaml", "--context", "default", *args])
    print(remote(runners()[0], [
        f"if systemctl is-active --quiet {TEST_UNIT}; then"
        f" echo '{TEST_UNIT} is still running: see test-log' >&2; exit 1; fi",
        # Releases shipped before k8s-current existed: fall back to the newest one.
        f"r=$(readlink -e {CURRENT} || ls -td /opt/sde/releases/*/ 2>/dev/null | head -1)",
        "[ -d \"$r\" ] || { echo 'no release shipped yet: run deploy' >&2; exit 1; }",
        "test -f $r/scripts/k8s_test.py || { echo \"no scripts/k8s_test.py in $r\" >&2; exit 1; }",
        f"systemd-run --unit {TEST_UNIT} --collect --working-directory=$r"
        f" --setenv=PATH=/usr/local/bin:/usr/bin:/bin python3 scripts/k8s_test.py {argv}",
        f"echo \"started {TEST_UNIT} from $r; follow it with test-log\"",
    ], timeout_s=60))


def cmd_test_log() -> None:
    print(remote(runners()[0], [
        f"systemctl is-active {TEST_UNIT} || true",
        f"journalctl -u {TEST_UNIT} -n 300 --no-pager -o cat",
    ], timeout_s=120))


def cmd_down() -> None:
    ids = runners()
    on_all(lambda i: remote(i, ["[ -x /usr/local/bin/k3s-agent-uninstall.sh ]"
                                " && /usr/local/bin/k3s-agent-uninstall.sh >/dev/null || true"],
                            timeout_s=600), ids[1:])
    remote(ids[0], ["[ -x /usr/local/bin/k3s-uninstall.sh ]"
                    " && /usr/local/bin/k3s-uninstall.sh >/dev/null || true"], timeout_s=600)
    print("K3s uninstalled on every runner. Compose is down: `make aws-deploy` restores it.")


COMMANDS = {"up": cmd_up, "deploy": cmd_deploy, "status": cmd_status, "logs": cmd_logs,
            "test": cmd_test, "test-log": cmd_test_log, "public": cmd_public, "down": cmd_down}


def main() -> None:
    args = sys.argv[1:]
    overlay = "aws"
    if "--overlay" in args:
        i = args.index("--overlay")
        if i + 1 >= len(args):
            sys.exit(f"--overlay takes one of {', '.join(OVERLAYS)}")
        overlay = args[i + 1]
        del args[i:i + 2]
    if not args or args[0] not in COMMANDS:
        sys.exit(__doc__)
    aws_ops.guard()
    if args[0] == "deploy":
        cmd_deploy(*args[1:2], overlay=overlay)
    else:
        COMMANDS[args[0]](*args[1:])


if __name__ == "__main__":
    main()
