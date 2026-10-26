# atlas-ops

[![CI](https://github.com/zhangyaoxing/atlas-ops/actions/workflows/ci.yml/badge.svg)](https://github.com/zhangyaoxing/atlas-ops/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/atlas-ops.svg)](https://pypi.org/project/atlas-ops/)

Command-line tool for MongoDB Atlas operations.

## Requirements

- [uv](https://docs.astral.sh/uv/)
- Python 3.10 (pinned in `.python-version`; uv selects it automatically)

## Quick start

```bash
# Create .venv and install runtime + development dependencies
uv sync

# Run the CLI
uv run atlas-ops --help
```

## Configuration

Put Atlas API credentials in `.env` (an API key from the Atlas Organization Access
Manager):

```bash
cp .env.example .env
# Edit .env and set ATLAS_PUBLIC_KEY / ATLAS_PRIVATE_KEY
```

You can also preset `ATLAS_PROJECT_ID` and `ATLAS_CLUSTER_NAME` in `.env`; they are
used when the corresponding options are omitted.

To avoid creating a temporary database user, provide an existing user through
`ATLAS_DB_USERNAME` / `ATLAS_DB_PASSWORD` (or `--db-username` / `--db-password`).
Both values are required; providing only one is an error. Passwords passed on the
command line end up in shell history, so prefer `.env` or environment variables.

At startup the tool searches upward from the current working directory for the
nearest `.env` and loads it. Variables already exported in the shell take precedence
over file values, so a temporary override can look like this:

```bash
ATLAS_PUBLIC_KEY=... ATLAS_PRIVATE_KEY=... uv run atlas-ops index ./examples/indexes \
  --project-id 64f1a2b3c4d5e6f7a8b9c0d1 --cluster cluster0
```

`.env` is ignored by git, so credentials are not committed accidentally. You can
also skip `.env` entirely and export the variables directly.

## Commands

### `index`

Read every JSON file in a directory (one file per collection), locate the cluster by
ID or name within the specified project, and select one of its connection strings.

The default mode creates missing indexes for the collections in the definitions,
finds existing indexes in those collections that are not present in the definitions,
and then prints a change preview table. It asks once whether to proceed; confirmed
deletions run after index creation, with no separate deletion prompt. With
`--export/-e`, `DIRECTORY` becomes an output directory: definitions are not read, no
index is created or deleted, and the index definitions of every collection in all
non-system databases are exported as JSON files.

```bash
# Project ID + cluster name
uv run atlas-ops index ./examples/indexes \
  --project-id 64f1a2b3c4d5e6f7a8b9c0d1 --cluster cluster0

# The same options in short form
uv run atlas-ops index ./examples/indexes \
  -g 64f1a2b3c4d5e6f7a8b9c0d1 -c cluster0

# A cluster ID (24 hexadecimal characters) also works
uv run atlas-ops index ./examples/indexes \
  --project-id 64f1a2b3c4d5e6f7a8b9c0d1 --cluster b1c2d3e4f5a6b7c8d9e0f1a2

# Read project and cluster from .env / environment variables
uv run atlas-ops index ./examples/indexes

# Use an existing database user and skip temporary-user create/wait/delete
uv run atlas-ops index ./examples/indexes \
  --project-id 64f1a2b3c4d5e6f7a8b9c0d1 --cluster cluster0 \
  --db-username appUser --db-password '...'

# Export every collection's index definitions to a directory (no create/delete)
uv run atlas-ops index ./exported-indexes \
  --project-id 64f1a2b3c4d5e6f7a8b9c0d1 --cluster cluster0 --export
```

Arguments:

- `DIRECTORY`: input directory containing `*.json` definitions in the default mode;
  output directory with `--export` mode (must already exist, not recursive)

Options:

- `-g, --project-id`: project ID (24 hexadecimal characters); falls back to
  `ATLAS_PROJECT_ID`, then prompts
- `-c, --cluster`: cluster ID or name; falls back to `ATLAS_CLUSTER_NAME`, then prompts
- `-e, --export`: export mode; does not create or delete any index
- `-u, --db-username` / `-p, --db-password`: existing database user; can also use
  `ATLAS_DB_USERNAME` / `ATLAS_DB_PASSWORD`. Both must be provided together. When
  supplied, no temporary user is created or deleted
- Passwords on the command line end up in shell history; prefer `.env` or environment
  variables

Cluster names are matched exactly first, then case-insensitively, and only within the
specified project.

Connection string fields can be strings, lists, or dictionaries. Strings are used
directly, lists are expanded item by item, and dictionaries are expanded value by
value (nested structures are expanded recursively). Only values starting with
`mongodb://` or `mongodb+srv://` are accepted. Every unique resulting connection
string is offered as a separate option.

JSON format:

```json
{
  "database": "exampleDB",
  "collection": "example",
  "indexes": [
    {
      "keys": { "field": 1 },
      "options": { "name": "field_index" },
      "commitQuorum": 1
    }
  ]
}
```

Rules:

- `database`, `collection`, and `indexes` are required; `indexes` must not be empty
- `keys` is required for every index; `options` and `commitQuorum` are optional
- Numeric `keys` directions support `1` / `-1` and the decimal forms `1.0` / `-1.0`;
  other numbers are rejected
- `keys` values can also be string index types such as `text` and `hashed`
- Unknown fields are rejected to catch typos early
- Validation errors from multiple files are reported together

Execution flow:

1. Read and validate every JSON file in the directory (skipped with `--export`)
2. Load the nearest `.env`, then read Atlas API credentials (shell variables win)
3. Resolve the project: `--project-id` → `ATLAS_PROJECT_ID` → interactive prompt
   (validated as 24 hexadecimal characters)
4. Resolve the cluster: `--cluster` → `ATLAS_CLUSTER_NAME` → interactive prompt; look
   it up by ID or name in the project, and prompt when multiple clusters share a name
5. Fetch cluster details and list all connection strings (strings, lists, and
   dictionaries are expanded into separate options; for example `standard`,
   `standardSrv`, `private`, `privateSrv`, `awsPrivateLink`, `privateEndpoint`);
   prompt when there is more than one
6. Database user: if `--db-username` / `--db-password` or the corresponding
   environment variables are provided, use that user; otherwise create a temporary
   user (`readWriteAnyDatabase`, scoped to the current cluster, with
   `deleteAfterDate` set to six hours from now)
7. Only a temporary user needs to wait for activation: poll authenticated
   `listDatabases` with the new credentials, retrying every two seconds on
   authentication failures (error codes 13/18) for up to 120 seconds
8. Connect to MongoDB with the database user and check whether each definition's
   `database` / `collection` exists; list missing databases or collections and ask
   whether to continue (default: no)
9. Match existing indexes by their ordered `keys` pattern (never by name):
   - `options` match exactly → log one INFO and skip
   - `options` differ → log a WARNING (with the existing name and both option sets)
     and skip
10. Find existing indexes in the defined collections that are not present in the
    definitions (again by `keys`; `_id_` is always excluded) and include them as
    planned deletions
11. Print a preview table of planned Created / Skipped / Deleted counts and ask
    `Proceed with these changes?` (default: no); declining aborts before any change
12. Run `createIndexes` for indexes that need creating, grouped by
    `(database, collection, commitQuorum)` with one command per group; the index name
    comes from `options.name`, or is generated using MongoDB rules (for example
    `a_1_b_-1`); `commitQuorum` is set at the command level
13. Run `dropIndexes` for each extra index after index creation finishes; no separate
    deletion confirmation is asked
14. Temporary user only: delete it through the Atlas API on normal exit, error, or
    abort (send DELETE without waiting for MongoDB-side activation; deletion failures
    only log a WARNING)
15. Print the cluster, project, connection string, and a per-collection Markdown
    table of Created / Skipped / Deleted; Skipped includes identical indexes and
    indexes skipped because their options conflict

Before changes are applied, the preview looks like this:

Index change preview (2 collection(s) from /path/to/definitions):

| Collection    | Created | Skipped | Deleted |
| ------------- | ------: | ------: | ------: |
| exampleDB.one |       1 |       1 |       0 |
| exampleDB.two |       0 |       1 |       1 |
| **Total**     |   **1** |   **2** |   **1** |

Proceed with these changes? [y/N]:

The default mode ends with output like this:

Index summary (2 collection(s) from /path/to/definitions):

| Collection    | Created | Skipped | Deleted |
| ------------- | ------: | ------: | ------: |
| exampleDB.one |       1 |       1 |       0 |
| exampleDB.two |       0 |       1 |       1 |
| **Total**     |   **1** |   **2** |   **1** |

Skipped = identical or options-conflict indexes.

In terminal output, non-zero Created numbers are green and non-zero Deleted numbers
are red; zero values and headers stay unstyled. Rows with Created or Deleted counts
are listed first, with the largest change counts first.

With `--export`, steps 1 (read definitions) and 8–14 (preview and apply/delete
indexes) are skipped. After connecting to MongoDB, the tool enters export mode,
cleans up the temporary user, and prints the export summary.

#### Export mode

`--export/-e` treats `DIRECTORY` as an output directory and writes index definitions
collection by collection:

- No `createIndexes` / `dropIndexes` is executed
- The project/cluster, connection-string, and temporary-user create/wait flow still
  runs (the temporary user is skipped when existing database credentials are
  supplied); any temporary user is deleted on exit as usual
- All non-system databases are scanned; `admin`, `local`, and `config` are skipped
- `system.*` collections and views are skipped
- Every collection with at least one non-`_id_` index produces one
  `<database>.<collection>.json` file
- Unsafe filename characters such as `/`, `\`, and `:` are replaced with `_`
- Collections with only the `_id_` index produce no file
- `options` keeps every field returned by `listIndexes` except the server-managed
  `v`, `key`, and `ns`; this includes `name`, `unique`, and metadata such as
  `textIndexVersion`
- Exported files use the same JSON shape as input definitions. Regular indexes
  generally can be reused directly; special index types such as text indexes are
  returned by `listIndexes` in MongoDB's internal form (for example `_fts` / `_ftsx`
  keys) and may need manual adjustment before reuse
- The run ends with a per-collection Markdown table showing the number of exported
  indexes and the total:

Export summary (2 collection(s) from /path/to/exported-indexes):

| Collection       | Exported |
| ---------------- | -------: |
| exampleDB.orders |        1 |
| exampleDB.users  |        2 |
| **Total**        |    **3** |

The default mode ends after creating/deleting indexes; export mode ends after writing
files and printing the summary. Creating a temporary database user requires an Atlas
API key with sufficient permissions (for example Project Owner or Project Database
Access Admin).

When comparing options, `v` and `key` are removed from the existing index's options.
If a definition does not specify `name`, the existing index's generated name is
ignored during comparison as well.

If you confirm continuation, MongoDB creates missing databases and collections
implicitly during index creation; aborting exits with code 1 (default mode only).

Temporary-user credentials exist only for the duration of the run and are never
printed in the summary. The six-hour `deleteAfterDate` remains as a fallback in case
the process is killed. When existing database credentials are supplied, no temporary
user is created or deleted.

### `search`

Synchronize local Atlas Search index definitions to one Atlas cluster. With
`--export`, write the cluster's search indexes to JSON files instead.

The default mode compares each local definition with the remote cluster:

- missing index → create
- changed `definition` or `type` → update
- identical → skip
- remote index in a defined collection but absent locally → planned deletion

The command uses the Atlas Admin API cluster-wide Search index endpoint, so it does
not connect to MongoDB or create a temporary database user.

```bash
# Sync local definitions to the cluster
uv run atlas-ops search ./search-indexes \
  --project-id 64f1a2b3c4d5e6f7a8b9c0d1 --cluster cluster0

# Export remote indexes to a directory
uv run atlas-ops search ./exported-search \
  --project-id 64f1a2b3c4d5e6f7a8b9c0d1 --cluster cluster0 --export
```

Arguments:

- `DIRECTORY`: input directory containing `*.json` definitions in the default mode;
  output directory with `--export` (must already exist)

Options:

- `-g, --project-id`: project ID; falls back to `ATLAS_PROJECT_ID`, then prompts
- `-c, --cluster`: cluster ID or name; falls back to `ATLAS_CLUSTER_NAME`, then prompts
- `-e, --export`: export remote indexes instead of syncing definitions

Local definition format (one file per collection):

```json
{
  "database": "sample_mflix",
  "collection": "movies",
  "indexes": [
    {
      "name": "default",
      "type": "search",
      "definition": {
        "mappings": { "dynamic": true }
      }
    }
  ]
}
```

- `name` is matched within `database.collection`
- `type` is `search` or `vectorSearch`
- `definition` contains the user definition (`mappings`, `analyzers`, `synonyms`,
  `storedSource`, `numPartitions`, etc.)
- Unknown fields and duplicate names/namespaces are rejected

Sync behavior:

- Fetches all remote indexes for the cluster and compares them by
  `(database, collection, name)`
- Detects remote indexes in collections that have local definitions but are not
  present locally and includes them as planned deletions
- Prints a change preview table and asks `Proceed with these changes?` (default: no);
  declining aborts before any create, update, or delete
- After approval, creates or updates declared indexes through the Atlas SDK and
  deletes planned extras; no separate deletion confirmation is asked
- Leaves remote indexes in collections without local definition files untouched
- The preview and the final result use the same table under `Search change preview`
  and `Search sync summary` headings, respectively:

  ```text
  Search change preview (1 collection(s) from /path/to/search-indexes):

  | Collection        | Created | Updated | Skipped | Deleted |
  | ----------------- | ------: | ------: | ------: | ------: |
  | exampleDB.example |       1 |       1 |       1 |       1 |
  | **Total**         |   **1** |   **1** |   **1** |   **1** |

  Proceed with these changes? [y/N]:
  ```

- The final summary is:

  ```text
  Search sync summary (1 collection(s) from /path/to/search-indexes):

  | Collection        | Created | Updated | Skipped | Deleted |
  | ----------------- | ------: | ------: | ------: | ------: |
  | exampleDB.example |       1 |       1 |       1 |       1 |
  | **Total**         |   **1** |   **1** |   **1** |   **1** |

  Search sync complete: 1 created, 1 updated, 1 skipped, 1 deleted.
  ```

Non-zero Created numbers are green, Updated numbers are yellow, and Deleted numbers
are red in terminal output; zero values, headers, and other cells stay unstyled.
Rows with Created, Updated, or Deleted counts are listed first, with the largest
change counts first.

#### Search export mode

`--export` writes one file per collection with the local definition shape:

- Calls `GET /groups/{groupId}/clusters/{clusterName}/search/indexes`
- Keeps only `name`, `type`, and `definition` (taken from `latestDefinition`)
- Omits server-managed fields such as `indexID`, `status`, `queryable`,
  `statusDetail`, and `latestDefinitionVersion`
- Supports both `search` and `vectorSearch` definitions
- Writes `<database>.<collection>.json`; collections without Search indexes produce
  no file
- Exported files can be edited and synced back with the default mode
- The run ends with an export summary table:

  ```text
  Search export summary (1 collection(s) from /path/to/exported-search):

  | Collection          | Exported |
  | ------------------- | -------: |
  | sample_mflix.movies |        1 |
  | **Total**           |    **1** |
  ```

The Atlas API key needs permission to read and update Search indexes, for example
Project Search Index Editor. No `--db-username` / `--db-password` options are needed;
the command never opens a MongoDB connection.

### `trigger`

Export App Services trigger definitions related to one Atlas cluster. The command
uses the SDK's App Services Admin client (`AtlasAppAdminClient`), so it needs Atlas
API credentials, a project ID, and a cluster; it does not connect to MongoDB or
create a temporary database user. Only export mode is implemented so far.

```bash
mkdir -p ./exported-triggers
uv run atlas-ops trigger ./exported-triggers \
  --project-id 64f1a2b3c4d5e6f7a8b9c0d1 \
  --cluster cluster0 \
  --export
```

Arguments:

- `DIRECTORY`: output directory for trigger JSON files (must already exist)

Options:

- `-g, --project-id`: project ID; falls back to `ATLAS_PROJECT_ID`, then prompts
- `-c, --cluster`: cluster ID or name; falls back to `ATLAS_CLUSTER_NAME`, then prompts
- `-e, --export`: export mode; required for now

Behavior:

- Logs in to the App Services Admin API with the Atlas API key pair
- Scans both standard apps and the special `atlas` product app
- Inspects each app's MongoDB data sources and keeps only those linked to the target
  cluster (matched by cluster name or ID)
- Keeps only `DATABASE` triggers on those data sources that specify both a database
  and a collection
- Resolves each trigger's associated function from `function_id`, `function_name`, or
  `event_processors.FUNCTION`, then fetches the full function definition
- Writes one file per trigger: `<database>.<collection>.<trigger>.json`
- Adds a `function` subdocument next to `trigger`
- Removes server-managed `_id` and `last_modified` values from both trigger and
  function definitions; other definition fields are preserved
- Includes `appId` and `appName` for context; the trigger `name` is the stable key
- If two apps expose the same trigger name for the same namespace, the app name is
  appended to the filename to avoid overwriting
- Collections without matching triggers produce no file
- Unsafe filename characters are replaced with `_`
- The run ends with a per-collection Markdown table and total count:

  ```text
  Trigger export summary (3 file(s) from /path/to/exported-triggers):

  | Collection        | Triggers |
  | ----------------- | -------: |
  | exampleDB.example |        2 |
  | exampleDB.other   |        1 |
  | **Total**         |    **3** |
  ```

Trigger file format (`exampleDB.example.on-insert.json`):

```json
{
  "database": "exampleDB",
  "collection": "example",
  "appId": "65f1a2b3c4d5e6f7a8b9c0d1",
  "appName": "my-app",
  "trigger": {
    "name": "on-insert",
    "type": "DATABASE",
    "disabled": false,
    "config": {
      "service_id": "...",
      "database": "exampleDB",
      "collection": "example",
      "operation_types": ["INSERT"]
    },
    "function_id": "..."
  },
  "function": {
    "name": "on-insert-function",
    "private": true,
    "source": "exports = function(changeEvent) { ... };"
  }
}
```

`function` is `null` if the trigger has no resolvable function. The function source
code is included so the trigger can be recreated together with its function. The
`appId`, `function_id`, and `config.service_id` values are environment-specific; a
future name-keyed sync can ignore them and match triggers by `trigger.name`.

The final summary line also reports file, collection, and app counts:

```text
Exported 3 trigger(s) in 3 file(s) from 2 collection(s) across 1 app(s) to /path/to/exported-triggers.
```

The Atlas API key needs App Services admin access for the project, for example
Project Owner.

## Logging

Logs use the standard-library `logging` module with
`rich.logging.RichHandler`; Rich is a direct project dependency. Logs go to stderr
while stdout is reserved for command results, so redirection keeps the two streams
separate. Rich colorizes output when the terminal supports it and honors `NO_COLOR`
(`NO_COLOR=1` disables colors).

All logs are in English and default to INFO level. Typical output:

```text
12:00:00 INFO     Found cluster cluster0 (64f1…) by name in project Project Atlas (…)
         INFO     Found 2 connection string(s) for cluster cluster0
         INFO       1. standard: "mongodb://…"
         INFO       2. standard_srv: "mongodb+srv://…"
         INFO     Selected connection string standard_srv: "mongodb+srv://…"
```

Cluster and project names are green, connection-string labels are cyan, and
connection strings themselves are wrapped in double quotes.

## VS Code debugging

The repository ships with a `.vscode/` configuration. After opening the project in
VS Code:

1. Install the recommended extensions (Python, Python Debugger, Pylance, Ruff)
2. Select `${workspaceFolder}/.venv/bin/python` as the interpreter (`settings.json`
   already sets this as the default)
3. Choose a configuration under Run and Debug:

- `index: interactive (env / prompts)`: uses `examples/indexes`; project/cluster come
  from `.env` or environment variables, and are prompted for in the integrated
  terminal when missing
- `index: directory + project + cluster`: prompts for directory, project ID, and
  cluster; blank project/cluster values fall back to environment variables or
  interactive prompts
- `pytest: current file`: debug the currently open test file
- `version` / `help`

All configurations use the integrated terminal (`console: integratedTerminal`), so
interactive CLI prompts work normally. Set breakpoints directly under
`src/atlas_ops/`; `justMyCode: true` is the default. To step into `pyatlassdk`,
set it to `false`.

`.env` is loaded by the program itself, so no extra `envFile` configuration is needed
for debugging.

## Development

```bash
uv run pytest        # run tests
uv run ruff check .  # lint
uv run ruff format . # format
```

To activate the virtual environment manually the first time:

```bash
source .venv/bin/activate
```

## Project structure

```text
.
├── pyproject.toml                    # project metadata, dependencies, tool config
├── uv.lock                           # locked dependency versions
├── .python-version                   # pins Python 3.10
├── pyrightconfig.json                # Pyright/Pylance configuration
├── .env.example                      # environment variable template (copy to .env)
├── .vscode/                          # VS Code debug, test, and format config
├── .github/
│   └── workflows/
│       └── ci.yml                    # GitHub Actions CI
├── LICENSE                           # project license
├── examples/indexes/                 # sample index definitions
│   ├── exampleDB.example.json
│   ├── sample_mflix.movies.json
│   ├── sample_mflix.sessions.json
│   ├── sample_mflix.theaters.json
│   └── sample_mflix.users.json
├── src/
│   └── atlas_ops/
│       ├── __init__.py               # version
│       ├── __main__.py               # python -m atlas_ops entry point
│       ├── cli.py                    # Typer app and command definitions
│       ├── atlas.py                  # credentials, cluster lookup, connection strings
│       ├── env.py                    # .env loading (search upward, do not override)
│       ├── filenames.py              # safe JSON export filenames
│       ├── index/
│       │   ├── __init__.py
│       │   ├── create.py             # batched createIndexes
│       │   ├── definitions.py        # load and validate index definition JSON
│       │   ├── drop.py               # dropIndexes for undeclared indexes
│       │   ├── export.py             # export index definitions by namespace
│       │   ├── mongo.py              # MongoDB connection, auth URI, namespace checks
│       │   └── plan.py               # index/option comparison and extra-index detection
│       ├── search/
│       │   ├── __init__.py
│       │   └── export.py             # export Atlas Search index definitions
│       ├── trigger/
│       │   ├── __init__.py
│       │   └── export.py             # export App Services trigger definitions
│       ├── log.py                    # logging config (RichHandler, stderr)
│       └── errors.py                 # user-facing error types
└── tests/
    ├── conftest.py                   # isolate working directory from a real .env
    ├── index/
    │   ├── test_create.py            # createIndexes grouping and command tests
    │   ├── test_definitions.py       # definition validation tests
    │   ├── test_drop.py              # dropIndexes command tests
    │   ├── test_export.py            # index export and filename tests
    │   ├── test_mongo.py             # MongoDB URI and namespace tests
    │   └── test_plan.py              # index/option comparison tests
    ├── search/
    │   └── test_search_export.py     # Atlas Search export tests
    ├── trigger/
    │   └── test_trigger_export.py    # App Services trigger export tests
    ├── test_cli.py                   # end-to-end CLI tests
    ├── test_atlas.py                 # Atlas helper tests
    ├── test_env.py                   # .env loading tests
    └── test_log.py                   # color/logging configuration tests
```

## Dependencies

`pyatlassdk` is installed from PyPI (`pyatlassdk>=0.1.0`), so no local checkout or
`[tool.uv.sources]` override is required. `uv sync` installs it together with the
other dependencies.

## Adding a new command

Add a command in `src/atlas_ops/cli.py`:

```python
@app.command()
def hello(name: str = typer.Argument(..., help="Name to greet.")) -> None:
    """Say hello."""
    typer.echo(f"Hello, {name}!")
```

Command groups (sub-applications) work like this:

```python
users_app = typer.Typer(help="Manage users.", no_args_is_help=True)
app.add_typer(users_app, name="users")


@users_app.command("list")
def list_users() -> None: ...
```

After adding an entry point, invoke it with `uv run atlas-ops hello World`; no
reinstall is needed while the package is installed in editable mode.
