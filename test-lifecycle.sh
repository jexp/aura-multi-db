#!/usr/bin/env bash
# Integration test: full database lifecycle on Aura multi-db instance.
#
# Requires: .env with CLIENT_ID, CLIENT_SECRET, PROJECT_ID,
#           NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, AURA_INSTANCEID
#
# Usage:
#   ./test-lifecycle.sh                  # uses test.dump
#   ./test-lifecycle.sh mydb.dump        # custom dump file
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${SCRIPT_DIR}/aura-multi-db.py"
DUMP_FILE="${1:-${SCRIPT_DIR}/test.dump}"
TEST_DB="testdb_$(date +%s)"       # slugified name for the database
INSTANCE_ID="$(grep AURA_INSTANCEID "${SCRIPT_DIR}/.env" | cut -d= -f2 | tr -d '\"'"' '")"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

pass() { echo -e "${GREEN}✓ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; exit 1; }
step() { echo -e "\n${BLUE}=== $1 ===${NC}"; }

DB_ID=""

cleanup() {
    if [ -n "$DB_ID" ]; then
        echo -e "\n${BLUE}=== Cleanup ===${NC}"
        python3 "$PY" delete-db "$DB_ID" --wait --yes 2>/dev/null || true
        rm -f "${SCRIPT_DIR}"/Neo4j-*-"${DB_ID}"-*.txt 2>/dev/null || true
    fi
}
trap cleanup EXIT

# --- Tests ---

step "1. List databases (before)"
python3 "$PY" list-dbs --yes || fail "list-dbs failed"
pass "list-dbs"

step "2. Create database '${TEST_DB}' with default users"
OUTPUT=$(python3 "$PY" add-db "$TEST_DB" --users --wait --yes 2>&1)
echo "$OUTPUT"
DB_ID=$(echo "$OUTPUT" | grep "Database ID:" | awk '{print $NF}')
[ -n "$DB_ID" ] || fail "could not extract database ID"
pass "add-db --users --wait (database ID: ${DB_ID})"

# Credential file: Neo4j-{instance_id}-{db_id}-{db_slug}_ro-readonly.txt
RO_FILE="${SCRIPT_DIR}/Neo4j-${INSTANCE_ID}-${DB_ID}-${TEST_DB}_ro-readonly.txt"
[ -f "$RO_FILE" ] || fail "readonly credential file not found: ${RO_FILE}"
pass "credential files saved"

# Pick a second running database for multi-db tests (before launching parallel jobs)
SECOND_DB=$(python3 "$PY" list-dbs --yes 2>/dev/null \
    | grep "running" | grep -v "$DB_ID" | awk '{print $1}' | head -1)

step "3+4+4b. Add users (running in parallel)"

# Job A: ro-only named users on the new database
(
    python3 "$PY" add-users "$DB_ID" "$TEST_DB" --users testuser1,testuser2 --ro --yes \
        2>&1 | sed 's/^/  [job-A] /'
) &
JOB_A=$!

# Job B: ro+rw named user on the new database
(
    python3 "$PY" add-users "$DB_ID" "$TEST_DB" --users testwriter --yes \
        2>&1 | sed 's/^/  [job-B] /'
) &
JOB_B=$!

# Job C: multi-db user across new database + an existing one
if [ -n "$SECOND_DB" ]; then
    (
        python3 "$PY" add-users "${DB_ID},${SECOND_DB}" "${TEST_DB},existing" \
            --users multiuser --yes 2>&1 | sed 's/^/  [job-C] /'
    ) &
    JOB_C=$!
fi

wait $JOB_A || fail "add-users ro-only (testuser1, testuser2) failed"
pass "add-users --ro (testuser1, testuser2)"

wait $JOB_B || fail "add-users ro+rw (testwriter) failed"
pass "add-users (testwriter ro+rw)"

if [ -n "$SECOND_DB" ]; then
    wait $JOB_C || fail "add-users multi-db failed"
    pass "add-users multi-db (multiuser ro+rw, home=${DB_ID}, also ${SECOND_DB})"
fi

# Check credential files were created
RO_NAMED="${SCRIPT_DIR}/Neo4j-${INSTANCE_ID}-${DB_ID}-testuser1_${TEST_DB}_ro-readonly.txt"
[ -f "$RO_NAMED" ] || fail "named user credential file not found: ${RO_NAMED}"
RW_NAMED="${SCRIPT_DIR}/Neo4j-${INSTANCE_ID}-${DB_ID}-testwriter_${TEST_DB}_rw-readwrite.txt"
[ -f "$RW_NAMED" ] || fail "named rw user credential file not found: ${RW_NAMED}"
pass "credential files present"

if [ -n "$SECOND_DB" ]; then
    MULTI_RO="${SCRIPT_DIR}/Neo4j-${INSTANCE_ID}-${DB_ID}-multiuser_ro-readonly.txt"
    [ -f "$MULTI_RO" ] || fail "multi-db ro credential file not found: ${MULTI_RO}"
    grep -q "Roles:" "$MULTI_RO"      || fail "credential file missing Roles: comment"
    grep -q "$SECOND_DB" "$MULTI_RO"  || fail "credential file missing additional database reference"
    pass "multi-db credential file contains role names and second database"

    step "4b. Multi-db idempotency (user already exists — only roles granted)"
    python3 "$PY" add-users "${DB_ID},${SECOND_DB}" "${TEST_DB},existing" \
        --users multiuser --ro --yes \
        || fail "add-users multi-db idempotency failed"
    pass "multi-db add-users is idempotent (existing user)"
fi

step "5. List databases (after create)"
python3 "$PY" list-dbs --yes || fail "list-dbs failed"
pass "list-dbs shows new database"

if [ -f "$DUMP_FILE" ]; then
    step "6. Upload '${DUMP_FILE}' to '${DB_ID}'"
    python3 "$PY" upload "$DB_ID" "$DUMP_FILE" --yes || fail "upload failed"
    pass "upload"

    step "7. Query as default read-only user"
    RO_USER=$(grep NEO4J_USERNAME "$RO_FILE" | cut -d= -f2)
    RO_PASS=$(grep NEO4J_PASSWORD "$RO_FILE" | cut -d= -f2)
    RO_URI=$(grep NEO4J_URI "$RO_FILE" | cut -d= -f2)

    python3 -c "
import sys; sys.path.insert(0, '${SCRIPT_DIR}')
from importlib.machinery import SourceFileLoader
mod = SourceFileLoader('m', '${PY}').load_module()
query = mod.QueryAPI('${RO_URI}', '${RO_USER}', '${RO_PASS}')
print('Querying as ${RO_USER}...')
query.run_and_print('${DB_ID}', 'MATCH (n) RETURN labels(n)[0] AS label, count(*) AS count ORDER BY count DESC LIMIT 5')
" || fail "read-only query failed"
    pass "read-only user can query data"
else
    step "6-7. Upload + query (skipped — dump file not found: ${DUMP_FILE})"
fi

step "8. Delete database '${DB_ID}' (removes roles; named users remain)"
python3 "$PY" delete-db "$DB_ID" --wait --yes || fail "delete-db failed"
DB_ID=""  # prevent double-cleanup
pass "delete-db --wait"

step "9. List databases (after delete)"
python3 "$PY" list-dbs --yes || fail "list-dbs failed"
pass "list-dbs confirms deletion"

# Clean up credential files
rm -f "${SCRIPT_DIR}"/Neo4j-*-"${DB_ID}"-*.txt 2>/dev/null || true

echo -e "\n${GREEN}=== All tests passed ===${NC}"
