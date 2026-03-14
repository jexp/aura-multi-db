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
TEST_DB="test_$(date +%s)"

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

pass() { echo -e "${GREEN}✓ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; exit 1; }
step() { echo -e "\n${BLUE}=== $1 ===${NC}"; }

# Track database ID (create-db returns a generated ID)
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

step "2. Create database '${TEST_DB}' (with users)"
OUTPUT=$(python3 "$PY" add-db "$TEST_DB" --users --yes 2>&1)
echo "$OUTPUT"
DB_ID=$(echo "$OUTPUT" | grep "Database ID:" | awk '{print $NF}')
[ -n "$DB_ID" ] || fail "could not extract database ID"
pass "add-db --users (database ID: ${DB_ID})"

# Verify credential files were written
RO_FILE="${SCRIPT_DIR}/Neo4j-$(grep AURA_INSTANCEID "${SCRIPT_DIR}/.env" | cut -d= -f2 | tr -d '\"'"'"'  ')-${DB_ID}-${TEST_DB}-readonly.txt"
[ -f "$RO_FILE" ] || fail "readonly credential file not found: ${RO_FILE}"
pass "credential files saved"

step "3. List databases (after create)"
python3 "$PY" list-dbs --yes || fail "list-dbs failed"
pass "list-dbs shows new database"

if [ -f "$DUMP_FILE" ]; then
    step "4. Upload '${DUMP_FILE}' to '${DB_ID}'"
    python3 "$PY" upload "$DB_ID" "$DUMP_FILE" --yes || fail "upload failed"
    pass "upload + verification query"

    step "5. Query as read-only user"
    # Read credentials from the generated file
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
    step "4-5. Upload + query (skipped — dump file not found: ${DUMP_FILE})"
fi

step "6. Delete database '${DB_ID}' (also removes users/roles)"
python3 "$PY" delete-db "$DB_ID" --wait --yes || fail "delete-db failed"
DB_ID=""  # prevent double-cleanup
pass "delete-db --wait"

step "7. List databases (after delete)"
python3 "$PY" list-dbs --yes || fail "list-dbs failed"
pass "list-dbs confirms deletion"

# Clean up credential files
rm -f "${SCRIPT_DIR}"/Neo4j-*-"${TEST_DB}"-*.txt 2>/dev/null || true

echo -e "\n${GREEN}=== All tests passed ===${NC}"
