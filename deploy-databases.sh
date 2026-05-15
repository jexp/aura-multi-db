#!/usr/bin/env bash
# deploy-databases.sh — generic multi-database deployment
#
# Scans a folder for *.backup / *.dump files, derives the database name from each
# filename (part before 'neo4j-2026', slugified), then:
#   1. Creates each database if absent (upload + generic ro/rw users), or
#      re-uploads the dump if the database already exists. (Parallel.)
#   2. Assigns named users across all databases, with the home-db first.
#      (RW users get both ro+rw accounts; RO users get ro only.)
#
# Per-database output is tee'd to <folder>/outputs/<slug>.log so that parallel
# invocations never overwrite each other.
#
# Usage:
#   ./deploy-databases.sh <folder> <home-db-slug> <rw-users> <ro-users>
#
# Arguments:
#   folder       Directory containing *.backup / *.dump files (default: ./databases)
#   home-db-slug Slug of the database that becomes the home DB for all named users
#                (default: c360)
#   rw-users     Comma-separated usernames to receive ro+rw access (default: "")
#   ro-users     Comma-separated usernames to receive ro-only access (default: "")
#
# Example:
#   ./deploy-databases.sh ./databases c360 \
#       "michael_h,luanne_m" \
#       "david_p,ed_s"
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${SCRIPT_DIR}/aura-multi-db.py"

FOLDER="${1:-${SCRIPT_DIR}/databases}"
HOME_DB="${2:-c360}"
RW_USERS="${3:-}"
RO_USERS="${4:-}"

OUTPUTS_DIR="${FOLDER}/outputs"
export NEO4J_CREDS_DIR="${FOLDER}/users"
mkdir -p "$OUTPUTS_DIR" "$NEO4J_CREDS_DIR"

GREEN='\033[0;32m'; RED='\033[0;31m'; BLUE='\033[0;34m'; YELLOW='\033[0;33m'; NC='\033[0m'
pass()  { echo -e "${GREEN}✓ $1${NC}"; }
fail()  { echo -e "${RED}✗ $1${NC}"; exit 1; }
step()  { echo -e "\n${BLUE}=== $1 ===${NC}"; }
warn()  { echo -e "${YELLOW}! $1${NC}"; }

slugify() {
    python3 -c "
import re, sys
s = sys.argv[1].lower()
s = re.sub(r'[^a-z0-9]+', '_', s)
print(s.strip('_'))
" "$1"
}

db_name_from_file() {
    local base
    base="$(basename "$1")"
    base="${base%.backup}"; base="${base%.dump}"
    local prefix
    prefix="$(python3 -c "
import re, sys
base = sys.argv[1]
m = re.search(r'neo4j.2026', base)
print(base[:m.start()] if m else base)
" "$base")"
    slugify "$prefix"
}

# --- Phase 1: Scan folder and resolve existing database IDs ------------------

step "Phase 1: Scanning '${FOLDER}' for backup/dump files"

# Collect files with NUL-separated find (safe for names with spaces)
declare -a BACKUP_FILES=()
while IFS= read -r -d $'\0' f; do
    BACKUP_FILES+=("$f")
done < <(find "$FOLDER" -maxdepth 1 \( -name "*.backup" -o -name "*.dump" \) -print0 | sort -z)

[ ${#BACKUP_FILES[@]} -gt 0 ] || fail "No *.backup or *.dump files found in '${FOLDER}'"
echo "  Found ${#BACKUP_FILES[@]} file(s)"

LIST_OUTPUT=$(python3 "$PY" list-dbs --yes 2>/dev/null)
echo "$LIST_OUTPUT"

declare -a DB_NAMES=()
declare -a EXISTING_IDS=()

for file in "${BACKUP_FILES[@]}"; do
    db_name=$(db_name_from_file "$file")
    DB_NAMES+=("$db_name")
    existing_id=$(echo "$LIST_OUTPUT" | awk -v n="$db_name" '$2 == n {print $1}')
    EXISTING_IDS+=("$existing_id")
    if [ -n "$existing_id" ]; then
        warn "$db_name already exists ($existing_id) — will re-upload"
    else
        echo "  $db_name — will create and upload"
    fi
done

# --- Phase 2: Create / upload in parallel ------------------------------------

step "Phase 2: Creating databases and uploading dumps (parallel)"
echo "  Logs → ${OUTPUTS_DIR}/<slug>.log"

PIDS=()

for i in "${!BACKUP_FILES[@]}"; do
    file="${BACKUP_FILES[$i]}"
    db_name="${DB_NAMES[$i]}"
    existing_id="${EXISTING_IDS[$i]}"
    logfile="${OUTPUTS_DIR}/${db_name}.log"

    if [ -n "$existing_id" ]; then
        (
            echo "Database ID: ${existing_id}"
            python3 "$PY" upload "$existing_id" "$file" --yes
        ) 2>&1 | tee "$logfile" | sed "s/^/  [${db_name}] /" &
    else
        (
            python3 "$PY" add-db "$db_name" --dump "$file" --users --wait --yes
        ) 2>&1 | tee "$logfile" | sed "s/^/  [${db_name}] /" &
    fi

    PIDS+=($!)
    echo "  Launched: $db_name (pid $!)"
done

# Collect results — continue even if some jobs failed so Phase 3 still runs
# for the successful databases.
declare -a ALL_DB_IDS=()
declare -a ALL_DB_NAMES=()
declare -a FAILED_DBS=()

for i in "${!PIDS[@]}"; do
    pid="${PIDS[$i]}"
    db_name="${DB_NAMES[$i]}"
    logfile="${OUTPUTS_DIR}/${db_name}.log"

    wait "$pid"
    exit_code=$?

    # Always try to extract the DB ID — the job may have created the DB
    # successfully even if it exited non-zero (e.g. upload failed after creation).
    db_id=$(grep "Database ID:" "$logfile" | tail -1 | awk '{print $NF}')

    if [ $exit_code -eq 0 ] && [ -n "$db_id" ]; then
        ALL_DB_IDS+=("$db_id")
        ALL_DB_NAMES+=("$db_name")
        pass "$db_name ($db_id) — log: outputs/${db_name}.log"
    else
        echo -e "${RED}✗ $db_name failed — see: outputs/${db_name}.log${NC}"
        FAILED_DBS+=("$db_name")
        # Still track the DB ID if we got one, so user assignment can include it
        if [ -n "$db_id" ]; then
            warn "  DB was created ($db_id) but upload failed — skipping from user assignment"
        fi
    fi
done

if [ ${#FAILED_DBS[@]} -gt 0 ]; then
    warn "Failed databases (re-upload with: python3 aura-multi-db.py upload <dbid> <file> --yes):"
    for db in "${FAILED_DBS[@]}"; do
        db_id=$(grep "Database ID:" "${OUTPUTS_DIR}/${db}.log" 2>/dev/null | tail -1 | awk '{print $NF}')
        warn "  $db  id=${db_id:-unknown}  log=outputs/${db}.log"
    done
fi

[ ${#ALL_DB_IDS[@]} -gt 0 ] || fail "All database operations failed — nothing to assign users to"

# --- Phase 3: Assign named users across all databases ------------------------

if [ -z "$RW_USERS" ] && [ -z "$RO_USERS" ]; then
    step "Phase 3: No named users specified — skipping"
else
    # Put home-db first so it becomes everyone's home database
    HOME_ID=""
    OTHER_IDS=()
    for i in "${!ALL_DB_IDS[@]}"; do
        if [[ "${ALL_DB_NAMES[$i]}" == "$HOME_DB" ]]; then
            HOME_ID="${ALL_DB_IDS[$i]}"
        else
            OTHER_IDS+=("${ALL_DB_IDS[$i]}")
        fi
    done
    [ -n "$HOME_ID" ] || fail "Home database '${HOME_DB}' not found among created databases"

    IDS_CSV="${HOME_ID}"
    for id in "${OTHER_IDS[@]}"; do IDS_CSV="${IDS_CSV},${id}"; done

    step "Phase 3: Assigning named users (home db: ${HOME_DB} / ${HOME_ID})"
    echo "  Databases: ${IDS_CSV}"
    [ -n "$RW_USERS" ] && echo "  RW users:  ${RW_USERS}"
    [ -n "$RO_USERS" ] && echo "  RO users:  ${RO_USERS}"

    JPIDS=()

    if [ -n "$RW_USERS" ]; then
        logfile="${OUTPUTS_DIR}/users_rw.log"
        (
            python3 "$PY" add-users "$IDS_CSV" --users "$RW_USERS" --yes
        ) 2>&1 | tee "$logfile" | sed 's/^/  [rw] /' &
        JPIDS+=("$!:rw:$logfile")
    fi

    if [ -n "$RO_USERS" ]; then
        logfile="${OUTPUTS_DIR}/users_ro.log"
        (
            python3 "$PY" add-users "$IDS_CSV" --users "$RO_USERS" --ro --yes
        ) 2>&1 | tee "$logfile" | sed 's/^/  [ro] /' &
        JPIDS+=("$!:ro:$logfile")
    fi

    for entry in "${JPIDS[@]}"; do
        pid="${entry%%:*}"; rest="${entry#*:}"; label="${rest%%:*}"; log="${rest#*:}"
        wait "$pid" || fail "${label} user assignment failed — see: $log"
        pass "${label} users assigned — log: ${log##*/outputs/}"
    done
fi

# --- Summary -----------------------------------------------------------------

echo -e "\n${GREEN}=== All done ===${NC}"
printf '  %-12s  %s\n' "DB ID" "Name"
printf '  %-12s  %s\n' "------------" "----"
for i in "${!ALL_DB_IDS[@]}"; do
    printf '  %-12s  %s\n' "${ALL_DB_IDS[$i]}" "${ALL_DB_NAMES[$i]}"
done
echo ""
echo "  Logs: ${OUTPUTS_DIR}/"
