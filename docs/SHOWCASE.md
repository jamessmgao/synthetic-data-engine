# The read-only showcase, and restoring from a backup

The finished run is served as a read-only site: the dashboard and the API on one small EC2
instance, with no Postgres and no Redis. This doc says how it is built and how to bring the
whole thing back from a backup.

## What the data is

| Piece | Size | Holds |
|---|---|---|
| The database | 9 MB as a gzipped `pg_dump`, 43 MB as SQLite | three tables: `batches`, `tasks`, `jobs` (every version, agent run, verdict and cost) |
| The artifacts | about 3 GB, 100,000 files | `bundles/`, `worlds/`, `drafts/`, `leak_reviews/`, `accepted/` (about 150 MB together) and `traces/` (2.8 GB) |

Nothing else is needed. Redis only indexes the queue, and the reconciler rebuilds it from the
database.

## Backups live in `.data/`

`.data/` is gitignored: the data is too large for git and is kept beside the code, not in it.

```
.data/
  postgres/<name>.sql.gz    a plain pg_dump of the database
  s3-artifacts/             a copy of the artifacts bucket, same folder names
```

To refresh it from AWS: `pg_dump` the database to `.data/postgres/`, and
`aws s3 sync s3://<artifacts-bucket>/ .data/s3-artifacts/`.

## Restore on a laptop

```bash
bash scripts/restore_local.sh        # .data/work/sde.db from the newest dump, artifacts linked in
env -u DATABASE_URL -u REDIS_URL -u S3_BUCKET SDE_WORK_DIR=$PWD/.data/work SDE_API_KEY=local \
  .venv/bin/python -m api            # the API on localhost:8000
make ui                              # the dashboard on localhost:3100 (web/.env.local: SDE_URL, SDE_API_KEY)
```

With `DATABASE_URL`, `REDIS_URL` and `S3_BUCKET` unset, the store is SQLite and the artifacts
are files under `SDE_WORK_DIR` (`shared/storage/__init__.py`). No service is needed. Every
page works, the trace viewer included.

`scripts/pg_to_sqlite.py` does the conversion: the store creates its own schema, and the
script copies the rows of the three tables out of the dump.

## The showcase on AWS

One `t4g.micro` (1 GB memory, 16 GB disk) runs three containers:

| Container | Runs | Reads |
|---|---|---|
| `api` | `python -m api` | `/data/sde.db`, and the artifacts from S3 (`S3_BUCKET`) |
| `dashboard` | the Next.js dashboard | the API, over the Docker network |
| `tunnel` | a Cloudflare quick tunnel | the dashboard only |

It is read-only by construction:
- only the dashboard is public, and its proxy (`web/app/api/sde`) forwards `GET` only;
- the API has no public address;
- the instance's role can read the artifacts bucket and nothing else.

To build it:
1. Build both images for ARM and pack them:
   `docker build -f deploy/Dockerfile --target control -t sde-pipeline:showcase .`,
   `docker build -f web/Dockerfile -t sde-dashboard:showcase web`,
   `docker save sde-pipeline:showcase sde-dashboard:showcase | gzip > images.tar.gz`.
2. Upload `images.tar.gz` and a gzipped `sde.db` to `s3://<artifacts-bucket>/showcase/`.
3. Launch the instance with `deploy/showcase/bootstrap.sh` as user data, an instance profile
   that allows `s3:GetObject` and `s3:ListBucket` on the bucket plus
   `AmazonSSMManagedInstanceCore`, and a metadata hop limit of 2 (the containers need the role).
4. Read the public URL from `docker logs tunnel`.

The URL of a quick tunnel changes whenever the tunnel restarts. A named Cloudflare tunnel on a
domain gives a fixed one.

## Generating again

The showcase cannot generate tasks: it has no workers. To generate, run the pipeline locally
(`make up`, see [DEPLOY.md](DEPLOY.md)) or deploy the full stack, then export the database
again and replace `sde.db`.
