# atlas-ops

[![CI](https://github.com/zhangyaoxing/atlas-ops/actions/workflows/ci.yml/badge.svg)](https://github.com/zhangyaoxing/atlas-ops/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/atlas-ops.svg)](https://pypi.org/project/atlas-ops/)

Command-line tool for MongoDB Atlas operations: synchronize and export indexes,
Atlas Search indexes, and App Services triggers.

Detailed documentation lives in the [project wiki](https://github.com/zhangyaoxing/atlas-ops/wiki).

## Requirements

- [uv](https://docs.astral.sh/uv/)
- Python 3.10

## Install

```bash
uv sync
uv run atlas-ops --help
```

## Configuration

Create `.env` from the template and set the Atlas API key pair:

```bash
cp .env.example .env
# ATLAS_PUBLIC_KEY=...
# ATLAS_PRIVATE_KEY=...
```

Optional environment variables:

| Variable | Used by | Description |
| --- | --- | --- |
| `ATLAS_PROJECT_ID` | all commands | Default project ID |
| `ATLAS_CLUSTER_NAME` | all commands | Default cluster name or ID |
| `ATLAS_CONN_TYPE` | `index` | Connection string type; skips the prompt |
| `ATLAS_DB_USERNAME` | `index` | Existing database user |
| `ATLAS_DB_PASSWORD` | `index` | Existing database password |

The nearest `.env` found by searching upward from the current directory is loaded.
Shell variables take precedence.

## Commands

### `index`

```bash
atlas-ops index DIRECTORY [OPTIONS]
```

`DIRECTORY` contains one JSON file per collection. By default, local definitions are
synchronized to MongoDB. With `--export`, MongoDB definitions are written to
`DIRECTORY` and stale local JSON files are removed.

| Option | Description |
| --- | --- |
| `-g, --project-id` | Atlas project ID; falls back to `ATLAS_PROJECT_ID` |
| `-c, --cluster` | Cluster name or ID; falls back to `ATLAS_CLUSTER_NAME` |
| `-e, --export` | Export from MongoDB to `DIRECTORY` instead of syncing |
| `-t, --connection-type` | Connection string type; falls back to `ATLAS_CONN_TYPE` |
| `-u, --db-username` | Existing database user; falls back to `ATLAS_DB_USERNAME` |
| `-p, --db-password` | Existing database password; falls back to `ATLAS_DB_PASSWORD` |

`--db-username` and `--db-password` must be provided together. Without them, a
temporary database user is created.

```bash
uv run atlas-ops index ./examples/indexes \
  --project-id 64f1a2b3c4d5e6f7a8b9c0d1 --cluster cluster0
```

### `search`

```bash
atlas-ops search DIRECTORY [OPTIONS]
```

`DIRECTORY` contains one JSON file per collection. By default, local Search index
definitions are synchronized to Atlas. With `--export`, Atlas definitions are written
to `DIRECTORY` and stale local JSON files are removed.

| Option | Description |
| --- | --- |
| `-g, --project-id` | Atlas project ID; falls back to `ATLAS_PROJECT_ID` |
| `-c, --cluster` | Cluster name or ID; falls back to `ATLAS_CLUSTER_NAME` |
| `-e, --export` | Export from Atlas to `DIRECTORY` instead of syncing |

```bash
uv run atlas-ops search ./search-indexes \
  --project-id 64f1a2b3c4d5e6f7a8b9c0d1 --cluster cluster0
```

### `trigger`

```bash
atlas-ops trigger DIRECTORY [OPTIONS]
```

`DIRECTORY` contains one JSON file per trigger. By default, local triggers and
functions are synchronized to the target project's `Triggers` app. With `--export`,
triggers are written to `DIRECTORY` and stale local JSON files are removed.

| Option | Description |
| --- | --- |
| `-g, --project-id` | Atlas project ID; falls back to `ATLAS_PROJECT_ID` |
| `-c, --cluster` | Cluster name or ID; falls back to `ATLAS_CLUSTER_NAME` |
| `-e, --export` | Export from App Services to `DIRECTORY` instead of syncing |

```bash
uv run atlas-ops trigger ./triggers \
  --project-id 64f1a2b3c4d5e6f7a8b9c0d1 --cluster cluster0
```

### `version`

```bash
atlas-ops version
atlas-ops --version
```

## Documentation

- [Getting Started](https://github.com/zhangyaoxing/atlas-ops/wiki/Getting-Started)
- [Index Command](https://github.com/zhangyaoxing/atlas-ops/wiki/Index-Command)
- [Search Command](https://github.com/zhangyaoxing/atlas-ops/wiki/Search-Command)
- [Trigger Command](https://github.com/zhangyaoxing/atlas-ops/wiki/Trigger-Command)
- [Development](https://github.com/zhangyaoxing/atlas-ops/wiki/Development)
