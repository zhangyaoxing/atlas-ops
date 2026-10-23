# sie-atlas-ops

Command-line tool for SIE Atlas operations.

## Requirements

- [uv](https://docs.astral.sh/uv/)
- Python 3.10 (selected automatically by uv from `.python-version`)

## Quick start

```bash
# Create .venv and install runtime + development dependencies
uv sync

# Run the CLI
uv run sie-atlas-ops --help
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
ATLAS_PUBLIC_KEY=... ATLAS_PRIVATE_KEY=... uv run sie-atlas-ops index ./examples/indexes \
  --project-id 64f1a2b3c4d5e6f7a8b9c0d1 --cluster cluster0
```

`.env` is ignored by git, so credentials are not committed accidentally. You can
also skip `.env` entirely and export the variables directly.

## Commands

### `index`

Read every JSON file in a directory (one file per collection), locate the cluster by
ID or name within the specified project, and select one of its connection strings.

The default mode creates missing indexes, finds existing indexes that are not
present in the definitions, lists them, and asks whether to delete them. Confirmed
deletions run after index creation. With `--export/-e`, `DIRECTORY` becomes an output
directory: definitions are not read, no index is created or deleted, and the index
definitions of every collection in the database are exported as JSON files.

```bash
# Project ID + cluster name
uv run sie-atlas-ops index ./examples/indexes \
  --project-id 64f1a2b3c4d5e6f7a8b9c0d1 --cluster cluster0

# The same options in short form
uv run sie-atlas-ops index ./examples/indexes \
  -g 64f1a2b3c4d5e6f7a8b9c0d1 -c cluster0

# A cluster ID (24 hexadecimal characters) also works
uv run sie-atlas-ops index ./examples/indexes \
  --project-id 64f1a2b3c4d5e6f7a8b9c0d1 --cluster b1c2d3e4f5a6b7c8d9e0f1a2

# Read project and cluster from .env / environment variables
uv run sie-atlas-ops index ./examples/indexes

# Use an existing database user and skip temporary-user create/wait/delete
uv run sie-atlas-ops index ./examples/indexes \
  --project-id 64f1a2b3c4d5e6f7a8b9c0d1 --cluster cluster0 \
  --db-username appUser --db-password '...'

# Export every collection's index definitions to a directory (no create/delete)
uv run sie-atlas-ops index ./exported-indexes \
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
`mongodb://` or `mongodb+srv://` are accepted. Every resulting connection string is
offered as a separate option.

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
10. Find existing indexes that are not present in the definitions (again by `keys`;
    `_id_` is always excluded), list them, and ask whether to delete them (default: no)
11. Run `createIndexes` for indexes that need creating, grouped by
    `(database, collection, commitQuorum)` with one command per group; the index name
    comes from `options.name`, or is generated using MongoDB rules (for example
    `a_1_b_-1`); `commitQuorum` is set at the command level
12. If deletion was confirmed, run `dropIndexes` for each extra index after index
    creation finishes
13. Temporary user only: delete it through the Atlas API on normal exit, error, or
    abort (send DELETE without waiting for MongoDB-side activation; deletion failures
    only log a WARNING)
14. Print the cluster, project, connection string, and a per-collection text table of
    Created / Skipped / Deleted; Skipped includes identical indexes and indexes
    skipped because their options conflict

The default mode ends with output like this:

```text
Index summary (2 collection(s) from /path/to/definitions):
Collection     Created  Skipped  Deleted
-------------  -------  -------  -------
exampleDB.one        1        1        0
exampleDB.two        0        1        1
-------------  -------  -------  -------
Total                1        2        1
Skipped = identical or options-conflict indexes.
```

With `--export`, steps 1 (read definitions) and 8–14 (apply/delete indexes) are
skipped. After connecting to MongoDB, the tool enters export mode, cleans up the
temporary user, and prints the export summary.

#### Export mode

`--export/-e` treats `DIRECTORY` as an output directory and writes index definitions
collection by collection:

- No `createIndexes` / `dropIndexes` is executed
- The project/cluster, connection-string, and temporary-user create/wait flow still
  runs; the temporary user is deleted on exit as usual
- Every database is scanned, excluding the `admin`, `local`, and `config` system
  databases
- `system.*` collections and views are skipped
- Every collection with at least one non-`_id_` index produces one
  `<database>.<collection>.json` file
- Unsafe filename characters such as `/`, `\`, and `:` are replaced with `_`
- Collections with only the `_id_` index produce no file
- `options` keeps `name`, `unique`, and other user options while removing the
  server-managed fields `v`, `key`, and `ns`
- Exported files can be used directly as the input directory for the default mode
- The run ends with a per-collection Markdown table showing the number of exported
  indexes and the total:

Export summary (2 collection(s) from /path/to/exported-indexes):

| Collection | Exported |
| --- | ---: |
| exampleDB.orders | 1 |
| exampleDB.users | 2 |
| **Total** | **3** |

The default mode ends after creating/deleting indexes; export mode ends after writing
files and printing the summary. Creating a temporary database user requires an Atlas
API key with sufficient permissions (for example Project Owner or Project Database
Access Admin).

When comparing options, the server-managed fields `v` and `key` are excluded. If a
definition does not specify `name`, the existing index's generated name is ignored
during comparison as well.

If you confirm continuation, MongoDB creates missing databases and collections
implicitly during index creation; aborting exits with code 1 (default mode only).

Temporary-user credentials exist only for the duration of the run and are never
printed in the summary. The six-hour `deleteAfterDate` remains as a fallback in case
the process is killed. When existing database credentials are supplied, no temporary
user is created or deleted.

## Logging

Logs use the standard-library `logging` module with
`rich.logging.RichHandler` (Rich is installed with Typer, so no extra dependency is
needed). Logs go to stderr while stdout is reserved for command results, so
redirection keeps the two streams separate. Rich colorizes output when the terminal
supports it and honors `NO_COLOR` (`NO_COLOR=1` disables colors).

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
`src/sie_atlas_ops/`; `justMyCode: true` is the default. To step into `pyatlassdk`,
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
├── .python-version                   # pins Python 3.10
├── .env.example                      # environment variable template (copy to .env)
├── .vscode/                          # VS Code debug, test, and format config
├── examples/indexes/example.json     # example index definition
├── src/
│   └── sie_atlas_ops/
│       ├── __init__.py               # version
│       ├── __main__.py               # python -m sie_atlas_ops entry point
│       ├── cli.py                    # Typer app and command definitions
│       ├── atlas.py                  # credentials, cluster lookup, connection strings
│       ├── env.py                    # .env loading (search upward, do not override)
│       ├── index/
│       │   ├── __init__.py
│       │   ├── create.py             # batched createIndexes
│       │   ├── definitions.py        # load and validate index definition JSON
│       │   ├── drop.py               # dropIndexes for undeclared indexes
│       │   ├── export.py             # export index definitions by namespace
│       │   ├── mongo.py              # MongoDB connection, auth URI, namespace checks
│       │   └── plan.py               # index/option comparison and extra-index detection
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
    ├── test_cli.py                   # end-to-end CLI tests
    ├── test_atlas.py                 # Atlas helper tests
    ├── test_env.py                   # .env loading tests
    └── test_log.py                   # color/logging configuration tests
```

## Dependencies

`pyatlassdk` points to a local checkout in editable mode through
`[tool.uv.sources]`:

```toml
pyatlassdk = { path = "../mongo-atlas-sdk/pyatlassdk", editable = true }
```

Adjust this line if your repository layout differs. Once the SDK is published to
PyPI, this can be replaced with a version constraint.

## Adding a new command

Add a command in `src/sie_atlas_ops/cli.py`:

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

After adding an entry point, invoke it with `uv run sie-atlas-ops hello World`; no
reinstall is needed while the package is installed in editable mode.
