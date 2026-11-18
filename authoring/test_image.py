"""The authoring image's Dockerfile, checked without building it. (A built image is
checked by scripts/check_authoring_image.sh: every tool on PATH in a login shell.)"""

from pathlib import Path

DOCKERFILE = (Path(__file__).resolve().parent / "Dockerfile").read_text().splitlines()


def test_login_shells_keep_the_image_path():
    """terminus-2 runs a login shell, where /etc/profile resets PATH: the image's ENV PATH
    (Go, the JDK) must be restored by a profile.d script written after the last ENV PATH."""
    env_path = max(i for i, line in enumerate(DOCKERFILE) if line.startswith("ENV PATH="))
    profile = [i for i, line in enumerate(DOCKERFILE) if "/etc/profile.d/00-sde-path.sh" in line
               and line.startswith("RUN ")]
    assert profile and profile[0] > env_path
    assert 'export PATH=\\"$PATH\\"' in DOCKERFILE[profile[0]]


def test_the_scaffold_is_a_real_directory():
    assert any("cp -r /opt/sde/authoring/scaffold /opt/sde/scaffold" in line for line in DOCKERFILE)
    assert not any("ln -s /opt/sde/authoring/scaffold" in line for line in DOCKERFILE)
