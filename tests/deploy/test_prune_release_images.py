"""deploy/prune_release_images.py: which release images a node deletes after an import."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "prune_release_images", Path(__file__).resolve().parents[2] / "deploy" / "prune_release_images.py")
prune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune)

R = "docker.io/library/sde-pipeline:"


def img(id_, *tags, size=1_090_000_000):
    return {"id": f"sha256:{id_}", "repoTags": list(tags), "size": str(size)}


IMAGES = [
    img("new", R + "72249d7a65ec", R + "local"),  # this release (also :local)
    img("prev", R + "1ea009b28a09"),              # the previous release
    img("api", R + "63a7ea569066"),               # an older release a running pod still uses
    img("init", R + "8da919c413bd"),              # used only by an exited init container
    img("old1", R + "35616118aec3"), img("old2", R + "5e1073985063"),
    img("auth", "docker.io/library/sde-authoring:dev"),
    img("dash", "docker.io/library/sde-dashboard:6d56823bdf69"),
    img("base", "docker.io/library/docker:27.3.1-dind"),
    img("mixed", R + "0a725f80210e", "docker.io/library/other:1"),  # also tagged as something else
]
CONTAINERS = [{"imageRef": "sha256:api", "image": {"image": "sha256:api"}, "state": "CONTAINER_RUNNING"},
              {"imageRef": "init", "state": "CONTAINER_EXITED"}]


def test_only_unused_old_releases_are_dropped():
    drop = {i["id"] for i in prune.to_drop(IMAGES, CONTAINERS, {"72249d7a65ec", "1ea009b28a09"})}
    assert drop == {"sha256:old1", "sha256:old2"}


def test_images_of_other_repos_are_never_touched_even_with_no_keep_tags():
    drop = {i["id"] for i in prune.to_drop(IMAGES, [], set())}
    assert not drop & {"sha256:auth", "sha256:dash", "sha256:base", "sha256:mixed"}
    assert "sha256:new" in drop  # (the deploy always passes this release's tag)


def test_a_kept_id_keeps_all_its_tags():
    # Deletion is by ID: :local shares the current release's ID, so it is kept with it.
    drop = prune.to_drop(IMAGES, CONTAINERS, {"72249d7a65ec"})
    assert all(R + "local" not in i["repoTags"] for i in drop)
