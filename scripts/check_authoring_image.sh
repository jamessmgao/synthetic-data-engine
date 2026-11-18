#!/usr/bin/env bash
# Check that an authoring image's tools are on PATH in a LOGIN shell, the way terminus-2
# runs its commands (`bash -l`: /etc/profile resets PATH, so the image's ENV PATH must be
# restored by /etc/profile.d/00-sde-path.sh). Exit 0 if every tool is found.
#
#   scripts/check_authoring_image.sh [image]     (default sde-authoring:dev)
set -euo pipefail
image="${1:-sde-authoring:dev}"
tools="go java javac sde-world sde-task python3 docker"
docker run --rm --entrypoint bash "$image" -lc "
  missing=0
  for t in $tools; do
    if p=\$(command -v \$t); then echo \"ok      \$t  \$p\"; else echo \"MISSING \$t\"; missing=1; fi
  done
  echo \"login PATH=\$PATH\"
  exit \$missing"
