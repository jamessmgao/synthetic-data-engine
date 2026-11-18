"""The pipeline on K3s across the AWS runners (wrapped by `make aws-k8s-*`; docs/K8S_PLAN.md).

    up        stop compose on every runner; K3s server on runners()[0], agents on the rest;
              build + import sde-pipeline:local on every node (crictl-checked); install KEDA
    deploy    ship REF (default HEAD), rebuild + import the image, apply infra/k8s/overlays/aws,
              wait for the rollouts. `--overlay aws-prod`: the real stores and model from SSM
              (Secret sde-secrets), the authoring image on every node, the dashboard, and an IMDS
              check. `--only ui,api,…` (aws-prod): build, apply and roll just those components;
              nothing else restarts. A deploy never restarts the tunnels, so their URLs stay.
    public    the API's and the dashboard's public URLs (`api: …`, `dashboard: …`)
    join      add runners that aren't nodes yet as K3s agents, with the live release's images;
              the server and existing nodes are untouched (`up` would rebuild them)
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
UI_IMAGE = "sde-dashboard:local"  # the aws-prod overlays' dashboard, built on the server only
# `deploy --only a,b`: component → the Deployments (app.kubernetes.io/name) it applies and rolls.
COMPONENTS = {"ui": ("dashboard", "dashboard-tunnel"), "api": ("api",), "control": ("control",),
              "validate": ("validate",), "executor": ("executor",), "generation": ("generation",),
              "spot": ("spot-termination",)}  # a DaemonSet with a public image: nothing to build
DAEMONSETS = ("spot-termination",)
# Quick tunnels: a restart gives a new URL, so a deploy restarts them only when their spec changes.
TUNNELS = ("public-tunnel", "dashboard-tunnel")
JOINING = "sde/joining"  # the taint `join` holds on a new node until its images are in
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


def ship(ref: str, server_only: bool = False, targets: list[str] | None = None) -> str:
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
    ]
    if server_only:  # the UI-only deploy: the cluster's release (CURRENT) stays as it was
        remote(runners()[0], unpack, timeout_s=600)
        print(f"shipped {sha[:12]} to the server")
        return release
    unpack.append(f"ln -sfn {release} {CURRENT}")
    on_all(lambda i: remote(i, unpack, timeout_s=600), targets or cluster_runners())
    print(f"shipped {sha[:12]} to {f'{len(targets)} runners' if targets else 'every runner'}")
    return release


def build_and_import(release: str, targets: list[str] | None = None) -> None:
    """Build the image into each node's Docker and copy it into K3s's containerd.

    Tagged both :local (the test overlays) and :<release sha> (aws-prod pins that: see
    pin_tags). `ctr import` can report success without the image landing, so crictl checks.
    """
    sha_image = f"sde-pipeline:{release_tag(release)}"
    cmds = [
        f"cd {release} && docker build -q --provenance=false --sbom=false"
        f" -f deploy/Dockerfile --target runner -t {IMAGE} -t {sha_image} . >/dev/null",
        f"docker save {IMAGE} {sha_image} | k3s ctr -n k8s.io images import - >/dev/null",
        f"k3s crictl images -q docker.io/library/{sha_image} | grep -q ."
        f" || {{ echo \"image {sha_image} missing on $(hostname) after import\" >&2; exit 1; }}",
        f"echo \"$(hostname): {sha_image} imported\"",
    ]
    for log in on_all(lambda i: remote(i, cmds, timeout_s=1800), targets or cluster_runners()):
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


def runner_ips(ids: list[str]) -> dict[str, str]:
    return dict(zip(ids, on_all(lambda i: remote(i, ["hostname -I | awk '{print $1}'"], timeout_s=120).strip(), ids)))


def node_ips(server: str) -> set[str]:
    """The K3s nodes' InternalIPs (empty when K3s isn't on the server yet)."""
    return set(remote(server, [f"{KUBECTL} get nodes -o jsonpath="
                               "'{range .items[*]}{.status.addresses[?(@.type==\"InternalIP\")].address} {end}'"
                               " 2>/dev/null || true"], timeout_s=120).split())


def joining_ips(server: str) -> set[str]:
    """The InternalIPs of nodes still tainted {JOINING}: a join that didn't finish."""
    out = remote(server, [f"{KUBECTL} get nodes -o jsonpath='{{range .items[*]}}"
                          "{.status.addresses[?(@.type==\"InternalIP\")].address}={.spec.taints[*].key};{end}'"
                          " 2>/dev/null || true"], timeout_s=120)
    return {ip for ip, _, taints in (e.partition("=") for e in out.strip().split(";") if e)
            if JOINING in taints.split()}


def cluster_runners() -> list[str]:
    """The runners that are K3s nodes: what a deploy ships to and builds on. A runner added by
    Terraform but not joined yet has no K3s to import into (`join` brings it in)."""
    ids = runners()
    known = node_ips(ids[0])
    if not known:
        return ids
    ips = runner_ips(ids)
    nodes = [i for i in ids if ips[i] in known]
    if len(nodes) < len(ids):
        print(f"skipping {len(ids) - len(nodes)} runner(s) not in the cluster yet: run `join`", flush=True)
    return nodes


def cmd_join() -> None:
    """Add runners that aren't K3s nodes yet (after raising runner_count) as agents, with the
    LIVE release's images, so they match the running pods. The server and the existing nodes
    are not touched: no compose stop, no rebuild, no restart, no KEDA reinstall (as `up` does)."""
    ids = runners()
    server = ids[0]
    ips = runner_ips(ids)
    # Not a node yet, or a node a cut-off join left tainted (its images may be missing): every
    # step below is idempotent, so a re-run resumes those (2026-11-18: an AWS token expired
    # mid-join and a re-run saw "already K3s nodes", leaving 8 Spot nodes tainted).
    unfinished = joining_ips(server)
    new = [i for i in ids if ips[i] not in node_ips(server) or ips[i] in unfinished]
    if not new:
        print(f"all {len(ids)} runners are already K3s nodes")
        return
    # The release the cluster runs, so the new nodes import the same sde-pipeline:local and
    # authoring tarball as the old ones (another ref would run other code on these nodes).
    sha = remote(server, [f"basename $(readlink -e {CURRENT})"], timeout_s=60).strip()
    print(f"joining {len(new)} runners {new} at the live release {sha[:12]}…", flush=True)
    release = ship(sha, targets=new)
    live = remote(server, [f"{K} exec deploy/generation -c app -- printenv SDE_AUTHORING_IMAGE"], timeout_s=120).strip()
    tag = authoring_image(release, targets=new)  # Docker only: no K3s needed yet
    if tag != live:
        sys.exit(f"the new nodes built {tag}, the cluster runs {live}: not joining them")
    # The agent first (the image import needs its containerd), tainted so no pod lands on the
    # node before sde-pipeline:local is in it. Each node is labelled sde/lifecycle=spot|on-demand.
    install = f"curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION={K3S_VERSION} K3S_TOKEN={TOKEN}"
    imds = "http://169.254.169.254/latest"
    on_all(lambda i: remote(i, [
        f"t=$(curl -s -X PUT {imds}/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')",
        f"lc=$(curl -s -H \"X-aws-ec2-metadata-token: $t\" {imds}/meta-data/instance-life-cycle)",
        "systemctl is-active --quiet k3s-agent || {"
        f" ip=$(hostname -I | awk '{{print $1}}');"
        f" {install} K3S_URL=https://{ips[server]}:6443 sh -s - agent --node-ip $ip"
        f" --node-label sde/lifecycle=${{lc:-unknown}} --node-taint {JOINING}:NoSchedule >/dev/null; }}",
    ], timeout_s=900), new)
    wait_ready(server, new, len(ids))
    build_and_import(release, targets=new)
    # After scoped deploys the Deployments can run different releases' sde-pipeline:<sha>:
    # build every one in use, from its release dir on the server, so any pod can land here.
    used = remote(server, [f"{K} get deploy -o jsonpath='{{..image}}' | tr ' ' '\\n'"
                           " | sed -n 's/^sde-pipeline:\\(.*\\)$/\\1/p' | sort -u"], timeout_s=60).split()
    for tag in sorted(set(used) - {"local", release_tag(release)}):
        full = remote(server, [f"basename $(ls -d /opt/sde/releases/{tag}* | head -1)"], timeout_s=60).strip()
        print(f"also sde-pipeline:{tag}, in use by a Deployment…", flush=True)
        build_and_import(ship(full, targets=new), targets=new)
    if set(used) - {"local", release_tag(release)}:  # ship() relinked CURRENT to the older one
        on_all(lambda i: remote(i, [f"ln -sfn {release} {CURRENT}"], timeout_s=60), new)
    # K3s applies --node-taint only when the node registers, so removing it once is enough.
    hosts = on_all(lambda i: remote(i, ["hostname"], timeout_s=60).strip(), new)
    print(remote(server, [*(f"{KUBECTL} taint node {h} {JOINING}:NoSchedule- || true" for h in hosts),
                          f"{KUBECTL} get nodes -L sde/lifecycle"], timeout_s=120))


def authoring_image(release: str, targets: list[str] | None = None) -> str:
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
    tags = {log.strip().splitlines()[-1]
            for log in on_all(lambda i: remote(i, cmds, timeout_s=1800), targets or cluster_runners())}
    if len(tags) != 1:
        sys.exit(f"the nodes disagree on the authoring image: {sorted(tags)}")
    tag = tags.pop()
    print(f"{tag} saved to {tar} on every node")
    return tag


def dashboard_image(release: str) -> None:
    """Build the dashboard (web/Dockerfile) on the server, where its pod is pinned, and import it
    into K3s. Docker's layer cache makes an unchanged web/ a no-op rebuild."""
    sha_image = f"sde-dashboard:{release_tag(release)}"
    print(remote(runners()[0], [
        f"cd {release} && docker build -q -t {UI_IMAGE} -t {sha_image} web >/dev/null",
        f"docker save {UI_IMAGE} {sha_image} | k3s ctr -n k8s.io images import - >/dev/null",
        f"k3s crictl images -q docker.io/library/{sha_image} | grep -q ."
        f" || {{ echo \"image {sha_image} missing after import\" >&2; exit 1; }}",
        f"echo \"{sha_image} imported on the server\"",
    ], timeout_s=1800).strip())


def release_tag(release: str) -> str:
    """The image tag of a release (/opt/sde/releases/<sha>): its sha's first 12 characters."""
    return release.rstrip("/").rsplit("/", 1)[-1][:12]


def pin_tags(release: str, images: tuple[str, ...]) -> list[str]:
    """Commands that point aws-prod's `images:` entries at this release's tags, in the release's
    copy of the overlay (as authoring.env is written there). A pod restarted later then runs
    the version it was deployed with, never whatever :local a later build left on its node.
    Idempotent (a release dir is reused when the same sha is deployed again); fails if the line
    isn't there, rather than deploying :local by accident."""
    k = "infra/k8s/overlays/aws-prod/kustomization.yaml"
    tag = release_tag(release)
    cmds = []
    for name in images:
        head = f"{{name: {name}, newName: {name}, newTag: "
        cmds += [f"grep -qF {shlex.quote(head)} {k} || {{ echo 'no {name} image line in {k}' >&2; exit 1; }}",
                 f"sed -i 's/{{name: {name}, newName: {name}, newTag: [^}}]*}}/{head}{tag}}}/' {k}",
                 f"grep -qF {shlex.quote(head + tag + '}')} {k} || {{ echo 'pinning {name} failed' >&2; exit 1; }}"]
    return cmds


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


def template_snapshot(sel: str) -> str:
    """Record each selected Deployment's pod-template hash before an apply (for
    restart_unchanged, in the same command list: SSM runs it as one shell script)."""
    return (f"tmpl=$(mktemp); for d in $({K} get deploy -l {shlex.quote(sel)} -o name); do"
            f" echo \"$d $({K} get $d -o jsonpath='{{.spec.template}}' | sha256sum | cut -c1-16)\"; done > $tmpl")


def restart_unchanged(sel: str) -> str:
    """Restart only the Deployments whose template the apply left as it was (same tag, new
    image contents). One the apply changed is already rolling: a restart would roll it twice."""
    return (f"for d in $({K} get deploy -l {shlex.quote(sel)} -o name); do"
            f" h=$({K} get $d -o jsonpath='{{.spec.template}}' | sha256sum | cut -c1-16);"
            f" if grep -qx \"$d $h\" $tmpl; then {K} rollout restart $d;"
            " else echo \"$d: rolling from the apply\"; fi; done; rm -f $tmpl")


def cmd_deploy_only(ref: str, path: str, only: list[str]) -> None:
    """A scoped deploy: build only the images these components use, apply only their objects
    (plus sde-config and the bootstrap Job for pipeline components), roll only their
    Deployments. Everything else, the tunnels' URLs included, is left as it is.

    Images: ui → sde-dashboard (web/, the server only); api / control / validate / executor →
    sde-pipeline (every node); generation → sde-pipeline plus the authoring tarball."""
    names = [n for c in only for n in COMPONENTS[c]]
    pipeline = [n for n in names if n not in (*COMPONENTS["ui"], *DAEMONSETS)]
    release = ship(ref, server_only=not pipeline)
    if "ui" in only:
        dashboard_image(release)
    pre = [f"cd {release} && touch deploy/local.env",
           # Only the images this deploy built get this release's tag; the objects of the
           # other components aren't applied, so they keep the tags they run.
           *pin_tags(release, (("sde-pipeline",) if pipeline else ()) + (("sde-dashboard",) if "ui" in only else ()))]
    if pipeline:
        build_and_import(release)
        pre += prod_secret_cmds()
    env = "infra/k8s/overlays/aws-prod/authoring.env"
    if "generation" in only:
        pre.append(f"echo SDE_AUTHORING_IMAGE={authoring_image(release)} > {env}")
    else:
        # Keep the live authoring tag: the tarball on the nodes is that one, and a pipeline pod
        # restarted here must not point generation's next restart at another.
        pre += [f"live=$({K} exec deploy/generation -c app -- printenv SDE_AUTHORING_IMAGE)",
                f'[ -n "$live" ] || {{ echo "cannot read the live SDE_AUTHORING_IMAGE" >&2; exit 1; }}',
                f"echo SDE_AUTHORING_IMAGE=$live > {env}"]
    sel = names + (["sde-config", "bootstrap"] if pipeline else [])
    rolled = [n for n in names if n not in (*TUNNELS, *DAEMONSETS)]
    print(f"deploying only {', '.join(names)} from {path}…", flush=True)
    rolled_sel = "app.kubernetes.io/name in (" + ",".join(rolled) + ")"
    print(remote(runners()[0], [
        *pre,
        *([f"{K} delete job bootstrap --ignore-not-found"] if pipeline else []),
        *([template_snapshot(rolled_sel)] if rolled else []),
        f"set -o pipefail;{KUBECTL} kustomize --load-restrictor LoadRestrictionsNone {path}"
        f" | {KUBECTL} apply -l {shlex.quote('app.kubernetes.io/name in (' + ','.join(sel) + ')')} -f -",
        *([f"{K} wait job/bootstrap --for=condition=complete --timeout=300s"] if pipeline else []),
        *([restart_unchanged(rolled_sel)] if rolled else []),
        *(f"{K} rollout status {'daemonset' if n in DAEMONSETS else 'deploy'}/{n} --timeout=1800s" for n in names),
    ], timeout_s=1800))
    cmd_public()


def cmd_deploy(ref: str = "HEAD", overlay: str = "aws", only: str = "") -> None:
    if overlay not in OVERLAYS:
        sys.exit(f"--overlay must be one of {', '.join(OVERLAYS)}")
    prod = overlay.startswith("aws-prod")
    path = f"infra/k8s/overlays/{overlay}"
    if only:
        parts = [p for p in only.split(",") if p]
        bad = [p for p in parts if p not in COMPONENTS]
        if bad or not parts or not prod:
            sys.exit(f"--only takes a comma list of {', '.join(COMPONENTS)}, with an aws-prod overlay")
        return cmd_deploy_only(ref, path, parts)
    release = ship(ref)
    build_and_import(release)
    pre = [f"cd {release} && touch deploy/local.env"]  # the base's optional settings file
    if prod:
        pre += pin_tags(release, ("sde-pipeline", "sde-dashboard"))
        dashboard_image(release)
        tag = authoring_image(release)
        # The overlay merges this into sde-config (agreed with K1); the file is gitignored.
        # aws-prod's configMapGenerator reads it, also when aws-prod-mock builds on aws-prod.
        pre += [f"test -d {path} || {{ echo 'no {path} in this release' >&2; exit 1; }}",
                f"echo SDE_AUTHORING_IMAGE={tag} > infra/k8s/overlays/aws-prod/authoring.env",
                *prod_secret_cmds()]
    not_tunnels = "app.kubernetes.io/name notin (" + ",".join(TUNNELS) + ")"
    print(f"deploying {path}…", flush=True)
    print(remote(runners()[0], [
        *pre,
        # Jobs are immutable: replace the bootstrap job so it runs again on every deploy.
        f"{K} delete job bootstrap --ignore-not-found",
        template_snapshot(not_tunnels),
        # pipefail: a rejected object fails the deploy here instead of being restarted on its
        # old spec below (how 082166f's generation change went missing).
        f"set -o pipefail; {KUBECTL} kustomize --load-restrictor LoadRestrictionsNone {path} | {KUBECTL} apply -f -",
        # Same tag, new image contents: restart so the pods pick up what was just imported.
        # Not the tunnels: their image is pinned, and a restart would change their public URL.
        restart_unchanged(not_tunnels),
        f"for s in $({K} get statefulset -o name); do {K} rollout status $s --timeout=300s; done",
        f"if {K} get job/bootstrap >/dev/null 2>&1; then"
        f" {K} wait job/bootstrap --for=condition=complete --timeout=300s; fi",
        f"for d in $({K} get deploy -o name); do {K} rollout status $d --timeout=1800s; done",
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
    """The public HTTPS links of the API and the dashboard (the aws-prod overlays' Cloudflare
    quick tunnels), one `name: URL` line each."""
    for name, tunnel in (("api", "public-tunnel"), ("dashboard", "dashboard-tunnel")):
        # A just-started tunnel takes a few seconds to log its URL.
        out = remote(runners()[0], [
            f"for _ in $(seq 20); do u=$({K} logs deploy/{tunnel} --tail=200 2>/dev/null"
            " | grep -o 'https://[a-z0-9-]*\\.trycloudflare\\.com' | tail -1);"
            " [ -n \"$u\" ] && break; sleep 3; done; echo $u"], timeout_s=120)
        print(f"{name}: {out.strip() or f'no URL yet (is deploy/{tunnel} running?)'}")


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
            "test": cmd_test, "test-log": cmd_test_log, "public": cmd_public, "join": cmd_join,
            "down": cmd_down}


def main() -> None:
    args = sys.argv[1:]
    overlay = "aws"
    if "--overlay" in args:
        i = args.index("--overlay")
        if i + 1 >= len(args):
            sys.exit(f"--overlay takes one of {', '.join(OVERLAYS)}")
        overlay = args[i + 1]
        del args[i:i + 2]
    only = ""
    if "--only" in args:
        i = args.index("--only")
        if i + 1 >= len(args):
            sys.exit(f"--only takes a comma list of {', '.join(COMPONENTS)}")
        only = args[i + 1]
        del args[i:i + 2]
    if not args or args[0] not in COMMANDS:
        sys.exit(__doc__)
    aws_ops.guard()
    if args[0] == "deploy":
        cmd_deploy(*args[1:2], overlay=overlay, only=only)
    else:
        COMMANDS[args[0]](*args[1:])


if __name__ == "__main__":
    main()
