# Neo4j Aura Multi-Database Manager

CLI tool for managing multiple databases on a single Neo4j Aura Business Critical instance via the v1beta6 API.

**No external dependencies** — pure Python 3.10+ using only `urllib` and standard library modules.

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
| `add-db <dbname> --users` | + create ro/rw users (implies --wait) | ~1 min |
| `add-db <dbname> --dump <file>` | + upload dump/backup (implies --users) | ~5 min |
| `list-dbs` | List all databases with status and counts | — |
| `upload <dbid> <file>` | Upload a .dump or .backup file | ~3 min |
| `delete-db <dbid>` | Delete database + associated users/roles | ~1 min |
| `add-users <dbid> [dbname]` | Create ro/rw users for existing database | — |
| `remove-users <dbid>` | Remove users/roles without deleting the database | — |

### Flags

| Flag | Description |
|------|-------------|
| `--wait` | Poll until async operations complete |
| `--yes` | Skip confirmation prompts (for scripting) |
| `--users` | Create ro/rw users after database is running |
| `--dump <file>` | Upload dump/backup after creating database |
| `--creds <file>` | Load credentials from file (overrides .env) |

### Credential files

`create-instance`, `add-db --users`, and `add-users` save credential files in the same format as Aura console exports:

```
Neo4j-{instanceid}-{dbid}-{dbname}-readonly.txt
Neo4j-{instanceid}-{dbid}-{dbname}-readwrite.txt
```

These files can be passed to other commands via `--creds`:

```bash
# Create instance, then add a database using the generated credentials
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

## Examples

```bash
# Full lifecycle: create database with users and load data
python3 aura-multi-db.py add-db sales --dump sales.dump --yes

# Quick async create (returns immediately)
python3 aura-multi-db.py add-db analytics

# List all databases
python3 aura-multi-db.py list-dbs

# Upload data to an existing database
python3 aura-multi-db.py upload abc123 data.dump

# Delete a database (removes users/roles too)
python3 aura-multi-db.py delete-db abc123 --wait --yes
```

## Integration test

```bash
./test-lifecycle.sh              # uses test.dump
./test-lifecycle.sh mydata.dump  # custom dump file
```

Runs the full lifecycle: list → add-db --users → list → upload → query as ro user → delete-db → list.

## Requirements

- Python 3.10+
- Neo4j Aura Business Critical instance with multi-database enabled (private preview)
- Aura API key (v1beta6)
