# Neo4j Aura Multi-Database Manager

CLI tool for managing multiple databases on a single Neo4j Aura Business Critical instance via the v1beta6 API.

**No external dependencies** — pure Python 3.9+ using only `urllib` and standard library modules.

## Setup

```bash
cp env.example .env
# Edit .env with your Aura API credentials and instance details
```

To get API credentials, create an API key in the [Aura Console](https://console.neo4j.io) under Account > API Keys.

## Usage

```
python3 aura-multi-db.py <command> [args...]
```

### Commands

| Command | Description | ~Time |
|---------|-------------|-------|
| `create-instance <name>` | Create a new multi-db BC instance | ~5 min |
| `add-db <dbname>` | Create a database (async, returns immediately) | — |
| `add-db <dbname> --wait` | Create + wait until running | ~1 min |
| `add-db <dbname> --users [prefix]` | + create ro/rw users (implies --wait) | ~1 min |
| `add-db <dbname> --dump <file>` | + upload dump/backup (implies --users) | ~5 min |
| `list-dbs` | List all databases with status and counts | — |
| `add-users <dbid>[,<dbid2>,...] [dbname[,...]] --users <user1,user2,...>` | Add named users to one or more databases | — |
| `upload <dbid> <file>` | Upload a .dump or .backup file | ~3 min |
| `delete-db <dbid>` | Delete database + associated users/roles | ~1 min |
| `remove-users <dbid>` | Remove users/roles without deleting the database | — |

### Flags

| Flag | Description |
|------|-------------|
| `--wait` | Poll until async operations complete |
| `--yes` | Skip confirmation prompts (for scripting) |
| `--users [prefix]` | (`add-db`) Create ro/rw users; prefix defaults to slugified dbname |
| `--users <user1,user2,...>` | (`add-users`) Comma-separated list of usernames to create |
| `--ro` | (`add-users`) Create only read-only users |
| `--rw` | (`add-users`) Create only read-write users |
| `--dump <file>` | Upload dump/backup after creating database |
| `--creds <file>` | Load credentials from file (overrides .env) |

### User and role naming

Database names passed to `add-db` are slugified (lowercase, alphanumeric, runs of other chars → `_`).
Roles are always tied to the hex database ID. User names embed the db slug so they are unique across databases:

| Command | Example users | Roles |
|---------|--------------|-------|
| `add-db c360_data --users` | `c360_data_ro`, `c360_data_rw` | `{dbid}_ro/rw` |
| `add-db c360_data --users michael` | `michael_c360_data_ro`, `michael_c360_data_rw` | `{dbid}_ro/rw` |
| `add-users <dbid> c360_data --users alice,bob` | `alice_c360_data_ro/rw`, `bob_c360_data_ro/rw` | `{dbid}_ro/rw` |
| `add-users <dbid> c360_data --users alice --ro` | `alice_c360_data_ro` only | `{dbid}_ro` |
| `add-users <dbid> --users alice,bob` | `alice_{dbid}_ro/rw`, `bob_{dbid}_ro/rw` | `{dbid}_ro/rw` |
| `add-users <db1>,<db2> name1,name2 --users michael --ro` | `michael_ro` (home db = db1, access to db1+db2) | `{db1}_ro`, `{db2}_ro` |

**Multi-database user behaviour:**
- If the user does not yet exist it is created with a fresh password and a credential file is saved.
- If the user already exists, only the new role grants are applied — the password is never changed and no new credential file is written.
- In multi-db mode the username has no db slug (`{user}_ro/rw`) and `SET HOME DATABASE` is set to the first listed database so clients land there by default.

All `CREATE ROLE` statements use `IF NOT EXISTS` so the command is safe to re-run.

### Credential files

`create-instance`, `add-db --users`, and `add-users` (for new users) save credential files:

```
Neo4j-{instanceid}-{dbid}-{username}-readonly.txt
Neo4j-{instanceid}-{dbid}-{username}-readwrite.txt
```

For multi-db users the file is keyed to the first database and includes comment lines listing all databases and the granted roles:

```
# Additional databases: <db2id>, <db3id>
# Roles: <db1id>_ro, <db2id>_ro
```

These files can be passed to other commands via `--creds`:

```bash
python3 aura-multi-db.py create-instance MyInstance
python3 aura-multi-db.py add-db mydb --users --creds Neo4j-abc123-Created-2026-03-14.txt
```

### Environment variables

Variables are loaded with increasing priority: **shell env < .env < --creds file**.

| Variable | Description |
|----------|-------------|
| `CLIENT_ID`, `CLIENT_SECRET` | Aura API credentials |
| `PROJECT_ID` | Aura project/tenant ID |
| `NEO4J_URI` | Instance bolt URI (`neo4j+s://...`) |
| `NEO4J_USERNAME`, `NEO4J_PASSWORD` | Admin credentials |
| `AURA_INSTANCEID` | Instance ID |
| `NEO4J_DATABASES_DIR` | Folder containing dump files (default: `databases`); credential files go to `<dir>/users/` |
| `NEO4J_CREDS_DIR` | Override credential file output dir directly (takes priority over `NEO4J_DATABASES_DIR`) |
| `GCP_CHUNK_SIZE_MB` | GCP upload chunk size in MB (default: 1024) |

## Examples

```bash
# Full lifecycle: create database with users and load data
python3 aura-multi-db.py add-db sales --dump sales.dump --yes

# Create database, wait, add specific named users (ro only)
python3 aura-multi-db.py add-db analytics --wait --yes
python3 aura-multi-db.py add-users c7511697 analytics --users alice,bob --ro

# Add both ro and rw users to existing database (with friendly name for slug)
python3 aura-multi-db.py add-users c7511697 c360_data --users michael,jane

# Grant michael read-only access to two databases (single user account, home db = first)
# Creates michael_ro with roles c7511697_ro and b9f63f7b_ro; home database = c7511697
python3 aura-multi-db.py add-users c7511697,b9f63f7b c360_data,analytics --users michael --ro

# Grant a team member ro+rw access across three databases
python3 aura-multi-db.py add-users c7511697,b9f63f7b,0bf4a21f c360,analytics,reporting --users alice

# Quick async create (returns immediately)
python3 aura-multi-db.py add-db reporting

# List all databases
python3 aura-multi-db.py list-dbs

# Upload data to an existing database
python3 aura-multi-db.py upload abc123 data.dump

# Delete a database (removes roles; named users from add-users remain)
python3 aura-multi-db.py delete-db abc123 --wait --yes
```

## Bulk deployment

`deploy-databases.sh` scans a folder for `*.backup` / `*.dump` files, creates or re-uploads each database in parallel, then assigns named users across all of them.

```bash
./deploy-databases.sh <folder> <home-db-slug> <rw-users> <ro-users>
```

| Argument | Description | Default |
|----------|-------------|---------|
| `folder` | Directory containing dump/backup files | `./databases` |
| `home-db-slug` | Slug of the database set as home DB for all named users | `c360` |
| `rw-users` | Comma-separated usernames to receive ro+rw access | _(none)_ |
| `ro-users` | Comma-separated usernames to receive ro-only access | _(none)_ |

```bash
# Deploy all dumps in ./databases, home db = mydb, with rw and ro user sets
./deploy-databases.sh ./databases mydb \
    "alice,bob" \
    "charlie,dana"
```

- Per-database logs are written to `<folder>/outputs/<slug>.log`
- Credential files go to `<folder>/users/`
- Databases are created/uploaded in parallel; user assignment runs after all uploads complete
- If some uploads fail, user assignment still runs for the successful ones
- Safe to re-run: existing databases are re-uploaded rather than re-created; existing users get new role grants without a password change

### Adding users to an existing deployment

To add more users to an already-deployed set of databases, run `add-users` directly with the comma-separated list of database IDs (home DB first):

```bash
# RO only
python3 aura-multi-db.py add-users <homeid>,<id2>,<id3>,... --users alice,bob --ro --yes

# RW (ro + rw accounts)
python3 aura-multi-db.py add-users <homeid>,<id2>,<id3>,... --users alice,bob --yes
```

Credential files are written to `./databases/users/` by default (override with `NEO4J_DATABASES_DIR` or `NEO4J_CREDS_DIR`).

## Integration test

```bash
./test-lifecycle.sh              # uses test.dump
./test-lifecycle.sh mydata.dump  # custom dump file
```

Runs the full lifecycle: list → add-db --users → parallel user additions (ro-only, ro+rw, multi-db) → list → upload → query as ro user → delete-db → list.

The three `add-users` steps (single-db ro, single-db ro+rw, multi-db across two databases) run in parallel to keep total test time down.

## Requirements

- Python 3.9+
- Neo4j Aura Business Critical instance with multi-database enabled
- Aura API key (v1beta6)
