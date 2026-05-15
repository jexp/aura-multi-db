#!/usr/bin/env python3
"""Manage multi-database instances and databases on Neo4j Aura (v1beta6 API).

Provides CLI commands for the full lifecycle: creating instances, databases,
users/roles, uploading dump/backup files, and teardown — all via HTTP APIs
with no external tool dependencies.
"""

import json
import os
import re
import secrets
import stat
import string
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import zlib
from base64 import b64encode
from pathlib import Path


# --- Configuration ---

AURA_API_BASE = "https://api.neo4j.io/v1beta6"
AURA_AUTH_URL = "https://api.neo4j.io/oauth/token"

INSTANCE_DEFAULTS = {
    "cloud_provider": "gcp",
    "region": "europe-west1",
    "memory": "4GB",
    "storage": "8GB",
    "version": "5",
    "type": "enterprise-db",
}

UPLOAD_MAX_RETRIES = 3
UPLOAD_MAX_BACKOFF_SECONDS = 64
IMPORT_POLL_INTERVAL_SECONDS = 10
IMPORT_POLL_MAX_ITERATIONS = 600  # ~20 min
DB_POLL_INTERVAL_SECONDS = 10
DB_POLL_MAX_ITERATIONS = 120  # ~10 min


# --- Utilities ---

def die(msg: str) -> None:
    """Print error message to stderr and exit."""
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def confirm(msg: str) -> None:
    """Prompt user to press enter or ctrl-c to abort. Skipped with --yes."""
    if "--yes" in sys.argv:
        if msg:
            print(msg)
        return
    input(f"{msg}\nPress enter to continue, ctrl-c to abort: ")


def has_flag(flag: str) -> bool:
    """Check if a flag like --wait or --yes is present in sys.argv."""
    return flag in sys.argv


def _is_interactive() -> bool:
    """Return True if stdout is a terminal (not piped/redirected)."""
    return sys.stdout.isatty()


def _print_elapsed(label: str, status: str, start: float) -> None:
    """Print a status line with elapsed time. Uses \\r overwrite on TTYs."""
    elapsed = int(time.time() - start)
    mins, secs = divmod(elapsed, 60)
    ts = f"{mins}:{secs:02d}"
    line = f"  {label}: {status} [{ts}]"
    if _is_interactive():
        # Clear entire line with ANSI escape, then write
        print(f"\r\033[K{line}", end="", flush=True)
    elif elapsed % 30 == 0:  # non-interactive: print every ~30s
        print(line)


def load_env(env_path: Path) -> dict:
    """Load key=value pairs from a .env file, stripping surrounding quotes."""
    env = {}
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        env[key.strip()] = val.strip().strip("\"'")
    return env


def _write_credential_file(filepath: Path, content: str) -> None:
    """Write a credential file with restricted permissions (owner-only)."""
    fd = os.open(filepath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                 stat.S_IRUSR | stat.S_IWUSR)  # 0600
    with os.fdopen(fd, "w") as f:
        f.write(content)


def require_env(env: dict, *keys: str) -> None:
    """Exit with an error if any of the given keys are missing from env."""
    missing = [k for k in keys if not env.get(k)]
    if missing:
        die(f"Missing required .env variables: {', '.join(missing)}")


def positional_args() -> list:
    """Return sys.argv without flags (--wait, --yes, --users, --creds/--dump <val>)."""
    result = []
    skip_next = False
    for arg in sys.argv:
        if skip_next:
            skip_next = False
            continue
        if arg in ("--creds", "--dump"):
            skip_next = True  # skip the next arg (the value)
            continue
        if arg.startswith("--"):
            continue
        result.append(arg)
    return result


def _parse_flag_value(flag: str) -> str:
    """Extract a --flag <value> or --flag=value from sys.argv, or return None."""
    for i, arg in enumerate(sys.argv):
        if arg == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if arg.startswith(f"{flag}="):
            return arg.split("=", 1)[1]
    return None


def get_arg(index: int, usage_hint: str) -> str:
    """Return positional arg at index (skipping flags) or die with usage."""
    args = positional_args()
    if len(args) > index:
        return args[index]
    die(f"Usage: {usage_hint}")


# --- HTTP helpers ---

def _basic_auth_header(username: str, password: str) -> str:
    """Build a Basic auth header value from credentials."""
    credentials = b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {credentials}"


def _json_request(url: str, method: str = "GET", headers: dict = None,
                  body: dict = None) -> dict:
    """Make an HTTP request with a JSON body and return parsed JSON response."""
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        try:
            return json.loads(error_body)
        except json.JSONDecodeError:
            die(f"HTTP {e.code}: {error_body}")


def _raw_request(url: str, method: str = "GET", headers: dict = None,
                 body_bytes: bytes = None) -> tuple:
    """Low-level HTTP request returning (status_code, response_headers, body_bytes).

    Does NOT raise on HTTP errors — the caller is responsible for
    interpreting the status code.
    """
    req = urllib.request.Request(url, data=body_bytes, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _console_base_url(bolt_uri: str) -> str:
    """Derive the Aura console base URL from a bolt URI.

    Examples:
        neo4j+s://dbid.databases.neo4j.io     -> https://console.neo4j.io
        neo4j+s://dbid-env.databases.neo4j.io  -> https://console-env.neo4j.io
    """
    match = re.match(
        r"(?:bolt(?:\+routing)?|neo4j(?:\+s|\+ssc)?)://([^-]+)(-(.+))?.databases.neo4j.io$",
        bolt_uri,
    )
    if not match:
        die(f"Cannot derive console URL from bolt URI: {bolt_uri}")
    environment = match.group(2) or ""
    return f"https://console{environment}.neo4j.io"


def _generate_password(length: int = 24) -> str:
    """Generate a secure random password with mixed character types."""
    alphabet = string.ascii_letters + string.digits + "-_"
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def _crc32_checksum(file_path: Path) -> int:
    """Compute CRC32 checksum of a file, reading in 1 MB chunks."""
    crc = 0
    with open(file_path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


# --- Aura API client (v1beta6) ---

class AuraAPI:
    """Client for the Neo4j Aura REST API (v1beta6).

    Handles OAuth token acquisition and provides methods for instance
    and database lifecycle operations.
    """

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None

    def _get_token(self) -> str:
        """Acquire an OAuth access token using client credentials grant."""
        if self._token:
            return self._token
        data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(AURA_AUTH_URL, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("Authorization", _basic_auth_header(self.client_id, self.client_secret))
        try:
            with urllib.request.urlopen(req) as resp:
                self._token = json.loads(resp.read().decode())["access_token"]
                return self._token
        except (urllib.error.HTTPError, KeyError) as e:
            die(f"Failed to get Aura access token: {e}")

    def _request(self, path: str, method: str = "GET", body: dict = None) -> dict:
        """Make an authenticated request to the Aura API."""
        url = f"{AURA_API_BASE}{path}"
        headers = {"Authorization": f"Bearer {self._get_token()}"}
        result = _json_request(url, method=method, headers=headers, body=body)
        # Check both lowercase keys (standard) and capitalized keys (403 gateway responses)
        if ("error" in result or "errors" in result
                or "Message" in result or "Reason" in result):
            die(json.dumps(result, indent=2))
        return result

    def create_instance(self, name: str, config: dict) -> dict:
        """Create a new multi-database Aura instance."""
        return self._request("/instances", method="POST", body={
            "version": config.get("version", INSTANCE_DEFAULTS["version"]),
            "region": config.get("region", INSTANCE_DEFAULTS["region"]),
            "memory": config.get("memory", INSTANCE_DEFAULTS["memory"]),
            "storage": config.get("storage", INSTANCE_DEFAULTS["storage"]),
            "name": name,
            "type": config.get("type", INSTANCE_DEFAULTS["type"]),
            "tenant_id": config["tenant_id"],
            "cloud_provider": config.get("cloud_provider", INSTANCE_DEFAULTS["cloud_provider"]),
            "multi_database": True,
        })

    def list_databases(self, instance_id: str) -> list:
        """List all databases in an instance."""
        return self._request(f"/instances/{instance_id}/databases").get("data", [])

    def create_database(self, instance_id: str, name: str) -> dict:
        """Create a new database within an instance."""
        result = self._request(f"/instances/{instance_id}/databases",
                               method="POST", body={"name": name})
        return result.get("data", result)

    def delete_database(self, instance_id: str, database_id: str) -> dict:
        """Delete a database from an instance."""
        result = self._request(f"/instances/{instance_id}/databases/{database_id}",
                               method="DELETE")
        return result.get("data", result)

    def get_database(self, instance_id: str, database_id: str) -> dict:
        """Get details of a specific database, including status."""
        result = self._request(f"/instances/{instance_id}/databases/{database_id}")
        return result.get("data", result)

    def get_database_status(self, instance_id: str, database_id: str) -> str:
        """Get the current status of a database, or 'not_found' if gone."""
        url = f"{AURA_API_BASE}/instances/{instance_id}/databases/{database_id}"
        headers = {"Authorization": f"Bearer {self._get_token()}"}
        status_code, _, body = _raw_request(url, headers=headers)
        if status_code == 404:
            return "not_found"
        if status_code != 200:
            return "unknown"
        data = json.loads(body.decode()).get("data", {})
        return data.get("status", "unknown")

    def wait_for_database(self, instance_id: str, database_id: str,
                          target_status: str = "running") -> None:
        """Poll until a database reaches the target status."""
        label = f"Waiting for '{database_id}'"
        start = time.time()
        _print_elapsed(label, "starting...", start)
        for _ in range(DB_POLL_MAX_ITERATIONS):
            status = self.get_database_status(instance_id, database_id)
            if status == target_status:
                if _is_interactive():
                    print()  # newline after \r
                print(f"  Database '{database_id}' is {target_status}.")
                return
            if "fail" in status.lower():
                if _is_interactive():
                    print()
                die(f"Database '{database_id}' entered failed state: {status}")
            _print_elapsed(label, status, start)
            time.sleep(DB_POLL_INTERVAL_SECONDS)
        die(f"Timed out waiting for database '{database_id}' to be {target_status}.")

    def wait_for_database_gone(self, instance_id: str, database_id: str) -> None:
        """Poll until a database is no longer listed."""
        label = f"Waiting for '{database_id}' deletion"
        start = time.time()
        _print_elapsed(label, "deleting...", start)
        for _ in range(DB_POLL_MAX_ITERATIONS):
            status = self.get_database_status(instance_id, database_id)
            if status == "not_found":
                if _is_interactive():
                    print()
                print(f"  Database '{database_id}' deleted.")
                return
            _print_elapsed(label, status, start)
            time.sleep(DB_POLL_INTERVAL_SECONDS)
        die(f"Timed out waiting for database '{database_id}' deletion.")


# --- Neo4j Query API client (v2) ---

class QueryAPI:
    """Client for the Neo4j Query API v2 (HTTPS on port 443).

    Executes Cypher statements against a specific database using
    the Query API rather than the Bolt protocol.
    """

    def __init__(self, bolt_uri: str, username: str, password: str):
        self.base_url = "https://" + bolt_uri.replace("neo4j+s://", "")
        self.auth_header = _basic_auth_header(username, password)

    def run(self, database: str, statement: str) -> dict:
        """Execute a single Cypher statement and return the response data."""
        url = f"{self.base_url}/db/{database}/query/v2"
        result = _json_request(url, method="POST",
                               headers={"Authorization": self.auth_header},
                               body={"statement": statement})
        if result.get("errors"):
            msgs = [f"  [{e.get('code', '')}] {e.get('message', '')}"
                    for e in result["errors"]]
            die("Cypher error:\n" + "\n".join(msgs))
        return result.get("data", {})

    def run_many(self, database: str, statements: list) -> None:
        """Execute multiple Cypher statements sequentially."""
        for statement in statements:
            self.run(database, statement)

    def run_and_print(self, database: str, statement: str) -> None:
        """Execute a Cypher statement and print results as a formatted table."""
        data = self.run(database, statement)
        fields = data.get("fields", [])
        values = data.get("values", [])
        if fields and values:
            print("  " + " | ".join(str(f) for f in fields))
            for row in values:
                print("  " + " | ".join(str(v) for v in row))


# --- Console Import API (upload protocol) ---

class ConsoleImporter:
    """Handles the Aura Console import protocol for uploading dump/backup files.

    Encapsulates the multi-step upload flow:
      1. Authenticate with the console import API
      2. Validate file size against instance limits
      3. Initiate a presigned upload (get cloud storage URI)
      4. Perform GCP resumable upload to the signed URI
      5. Signal upload completion
      6. Poll until the database import finishes
    """

    def __init__(self, console_base: str, database_id: str,
                 username: str, password: str):
        self.console_base = console_base
        self.database_id = database_id
        self.username = username
        self.password = password
        self._bearer_token = None

    def _url(self, path: str) -> str:
        """Build a console API URL for this database."""
        return f"{self.console_base}/v1/databases/{self.database_id}/{path}"

    def _import_url(self) -> str:
        """Build the v2 import initiation URL."""
        return f"{self.console_base}/v2/databases/{self.database_id}/import"

    def _auth_headers(self) -> dict:
        """Return headers with the bearer token for authenticated requests."""
        return {
            "Authorization": f"Bearer {self._bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def authenticate(self) -> None:
        """Authenticate with the console import API using Neo4j credentials."""
        print("Authenticating with console...")
        status, _, body = _raw_request(
            self._url("import/auth"), method="POST",
            headers={
                "Authorization": _basic_auth_header(self.username, self.password),
                "Accept": "application/json",
                "Confirmed": "true",
            },
            body_bytes=b"",
        )
        if status != 200:
            die(f"Console auth failed (HTTP {status}): {body.decode()}")
        self._bearer_token = json.loads(body.decode())["Token"]
        print("  Authenticated.")

    def check_size(self, file_size: int) -> None:
        """Validate that the file size is within the instance's import limits."""
        status, _, body = _raw_request(
            self._url("import/size"), method="POST",
            headers=self._auth_headers(),
            body_bytes=json.dumps({"FullSize": file_size}).encode(),
        )
        if status != 200:
            die(f"Size check failed (HTTP {status}): {body.decode()}")
        print("  Size check passed.")

    def initiate_upload(self, crc32: int, file_size: int) -> tuple:
        """Request a presigned upload URI from the console.

        Returns (provider, signed_uri) — e.g. ("GCP", "https://storage...").
        """
        status, _, body = _raw_request(
            self._import_url(), method="POST",
            headers=self._auth_headers(),
            body_bytes=json.dumps({
                "Crc32": crc32,
                "DumpSize": file_size,
                "FullSize": file_size,
            }).encode(),
        )
        if status not in (200, 202):
            die(f"Initiate upload failed (HTTP {status}): {body.decode()}")

        upload_info = json.loads(body.decode())
        provider = upload_info.get("Provider", "GCP")
        signed_uri = (upload_info.get("SignedURI")
                      or (upload_info.get("SignedLinks") or [None])[0])
        if not signed_uri:
            die(f"No signed URI in response: {upload_info}")

        print(f"  Upload initiated (provider: {provider}).")
        return provider, signed_uri

    def signal_complete(self, crc32: int) -> None:
        """Notify the console that the file upload has finished."""
        status, _, body = _raw_request(
            self._url("import/upload-complete"), method="POST",
            headers=self._auth_headers(),
            body_bytes=json.dumps({"Crc32": crc32}).encode(),
        )
        if status != 200:
            die(f"Upload-complete signal failed (HTTP {status}): {body.decode()}")
        print("  Upload complete signal sent.")

    def poll_import_status(self) -> None:
        """Poll the import status until the database is running or fails."""
        label = f"Importing '{self.database_id}'"
        start = time.time()
        _print_elapsed(label, "uploading...", start)
        status_url = self._url("import/status")
        for _ in range(IMPORT_POLL_MAX_ITERATIONS):
            time.sleep(IMPORT_POLL_INTERVAL_SECONDS)
            status_code, _, body = _raw_request(
                status_url, method="GET",
                headers={"Authorization": f"Bearer {self._bearer_token}"},
            )
            if status_code != 200:
                _print_elapsed(label, f"HTTP {status_code}, retrying", start)
                continue

            status_body = json.loads(body.decode())
            db_status = status_body.get("Status", "unknown")

            if db_status == "running":
                if _is_interactive():
                    print()
                print(f"  Import complete — database '{self.database_id}' is running.")
                return
            if "fail" in db_status.lower():
                if _is_interactive():
                    print()
                die(f"Import failed: {db_status} — {status_body.get('Error', '')}")
            _print_elapsed(label, db_status, start)

        die("Import timed out after 20 minutes. Check console for status.")


# --- GCP resumable upload ---

def _gcp_initiate_session(signed_uri: str) -> str:
    """Start a GCP resumable upload session and return the upload URL."""
    req = urllib.request.Request(signed_uri, data=b"", method="POST")
    req.add_header("x-goog-resumable", "start")
    req.add_header("Content-Length", "0")
    req.add_header("Content-Type", "")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.headers["Location"]
    except urllib.error.HTTPError as e:
        if e.code == 201:
            return e.headers["Location"]
        die(f"Failed to initiate GCP resumable upload (HTTP {e.code}): {e.read().decode()}")


def _gcp_query_position(upload_url: str, file_size: int) -> int:
    """Ask GCP how many bytes have been received so far.

    Returns file_size if the upload is already complete, 0 if no bytes
    have been received, or the byte offset to resume from.
    """
    req = urllib.request.Request(upload_url, data=b"", method="PUT")
    req.add_header("Content-Length", "0")
    req.add_header("Content-Range", f"bytes */{file_size}")
    try:
        with urllib.request.urlopen(req):
            return file_size  # 200/201 means upload is complete
    except urllib.error.HTTPError as e:
        if e.code == 308:
            range_header = e.headers.get("Range", "")
            if not range_header:
                return 0
            return int(range_header.split("-")[1]) + 1
        die(f"Failed to get resume position (HTTP {e.code})")


def _gcp_send_chunk(upload_url: str, file_path: Path,
                    file_size: int, position: int) -> bool:
    """Upload file data from the given position. Returns True if complete."""
    remaining = file_size - position
    headers = {"Content-Length": str(remaining)}
    if position > 0:
        headers["Content-Range"] = f"bytes {position}-{file_size - 1}/{file_size}"

    with open(file_path, "rb") as f:
        f.seek(position)
        data = f.read()

    req = urllib.request.Request(upload_url, data=data, method="PUT")
    for key, value in headers.items():
        req.add_header(key, value)

    try:
        with urllib.request.urlopen(req):
            return True
    except urllib.error.HTTPError as e:
        if e.code == 308:
            return False  # incomplete, caller will query position
        raise


def _gcp_resumable_upload(signed_uri: str, file_path: Path, file_size: int) -> None:
    """Perform a GCP resumable upload with automatic retry on failures."""
    print("  Starting GCP resumable upload...")
    upload_url = _gcp_initiate_session(signed_uri)

    position = 0
    retry_count = 0

    while position < file_size:
        try:
            if _gcp_send_chunk(upload_url, file_path, file_size, position):
                if _is_interactive():
                    print(f"\r\033[K", end="")
                print(f"  Upload complete ({file_size} bytes).")
                return
            # 308 incomplete — query actual position
            position = _gcp_query_position(upload_url, file_size)
            percent = (position / file_size) * 100
            if _is_interactive():
                print(f"\r\033[K  Upload: {percent:.1f}% ({position}/{file_size})", end="", flush=True)
            else:
                print(f"  Upload: {percent:.1f}% ({position}/{file_size})")
            retry_count = 0

        except urllib.error.HTTPError as e:
            if e.code in (500, 502, 503, 504):
                retry_count += 1
                if retry_count > UPLOAD_MAX_RETRIES:
                    die("Upload failed after too many retries.")
                backoff = min(2 ** retry_count, UPLOAD_MAX_BACKOFF_SECONDS)
                print(f"  Server error {e.code}, retrying in {backoff}s...")
                time.sleep(backoff)
                position = _gcp_query_position(upload_url, file_size)
            else:
                die(f"Upload failed (HTTP {e.code}): {e.read().decode()}")


# --- Commands ---

def usage():
    """Print usage information and exit."""
    print("""Usage: aura-multi-db.py <command> [args...]

Manages multi-database instances and databases on Neo4j Aura (v1beta6 API).

Commands:                                                          ~time
  create-instance <name>             Create a new multi-db instance   ~5 min
                                       → saves Neo4j-{id}-Created-{date}.txt
  add-db <dbname>                    Create a database (async)
  add-db <dbname> --wait             Create + wait until running       ~1 min
  add-db <dbname> --users            + create ro/rw users              ~1 min
                                       → saves Neo4j-{inst}-{dbid}[-{name}]-{ro|rw}.txt
  add-db <dbname> --dump <file>      + upload dump/backup              ~5 min
                                       (implies --users --wait)
  list-dbs                           List databases with status
  add-users <dbid> [dbname]          Create ro/rw users (standalone)
                                       → saves Neo4j-{inst}-{dbid}[-{name}]-{ro|rw}.txt
  upload <dbid> <dump-or-backup>     Upload dump/backup file           ~3 min
  delete-db <dbid>                   Delete database + users/roles     ~1 min
  remove-users <dbid>                Remove users/roles only

Flags:
  --wait           Poll until async operations complete (add-db, delete-db)
  --yes            Skip confirmation prompts (for scripting)
  --users          Create ro/rw users after database is running (implies --wait)
  --dump <file>    Upload dump/backup after creating (implies --users --wait)
  --creds <file>   Load instance credentials from file (overrides .env)

Environment variables (shell, .env, or --creds file — last wins):
  CLIENT_ID, CLIENT_SECRET        Aura API credentials (v1beta6)
  PROJECT_ID                      Aura project/tenant ID
  NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD  Admin connection to the instance
  AURA_INSTANCEID                 Instance ID

  Priority: shell env < .env < --creds file""")
    sys.exit(1)


def cmd_create_instance(env: dict):
    """Create a new multi-database Business Critical Aura instance."""
    name = get_arg(2, "create-instance <name>")
    config = {
        "tenant_id": env["PROJECT_ID"],
        "cloud_provider": env.get("CLOUD_PROVIDER", INSTANCE_DEFAULTS["cloud_provider"]),
        "region": env.get("REGION", INSTANCE_DEFAULTS["region"]),
        "memory": env.get("MEMORY", INSTANCE_DEFAULTS["memory"]),
        "storage": env.get("STORAGE", INSTANCE_DEFAULTS["storage"]),
        "version": env.get("NEO4J_VERSION", INSTANCE_DEFAULTS["version"]),
        "type": env.get("INSTANCE_TYPE", INSTANCE_DEFAULTS["type"]),
    }

    print(f"Creating multi-db Aura instance '{name}'... (typically ~5 min)")
    print(f"  Type: {config['type']}, Memory: {config['memory']}, Storage: {config['storage']}")
    print(f"  Region: {config['cloud_provider']}/{config['region']}, Neo4j v{config['version']}")
    print(f"  Project: {config['tenant_id']}")
    confirm("")

    api = AuraAPI(env["CLIENT_ID"], env["CLIENT_SECRET"])
    data = api.create_instance(name, config)["data"]

    print(f"\n=== Instance Created ===")
    print(f"Instance ID:  {data['id']}")
    print(f"Name:         {data['name']}")
    print(f"Connection:   {data['connection_url']}")
    print(f"Username:     {data['username']}")
    print(f"Password:     (saved to credentials file)")
    print(f"Tenant:       {data['tenant_id']}")

    # Extract default database ID from connection URL
    connection_url = data['connection_url']
    default_db = connection_url.replace("neo4j+s://", "").split(".")[0]

    # Write credentials file (same format as Aura console export)
    script_dir = Path(__file__).parent
    filename = f"Neo4j-{data['id']}-Created-{time.strftime('%Y-%m-%d')}.txt"
    filepath = script_dir / filename
    _write_credential_file(filepath,
        f"# Wait 60 seconds before connecting using these details, "
        f"or login to https://console.neo4j.io to validate the Aura Instance is available\n"
        f"NEO4J_URI={connection_url}\n"
        f"NEO4J_USERNAME={data['username']}\n"
        f"NEO4J_PASSWORD={data['password']}\n"
        f"NEO4J_DATABASE={default_db}\n"
        f"AURA_INSTANCEID={data['id']}\n"
        f"AURA_INSTANCENAME={data['name']}\n"
    )
    print(f"\n  Saved {filepath.name}")


def cmd_add_db(env: dict):
    """Create a new database, optionally wait, add users, and upload a dump.

    Flags escalate: --dump implies --users, --users implies --wait.
    Without flags, the API call returns immediately (async).
    """
    database_name = get_arg(2, "add-db <dbname> [--wait] [--users] [--dump <file>]")
    require_env(env, "AURA_INSTANCEID")
    instance_id = env["AURA_INSTANCEID"]

    dump_file = _parse_flag_value("--dump")
    want_users = has_flag("--users") or dump_file
    want_wait = has_flag("--wait") or want_users

    print(f"Creating database '{database_name}' in instance {instance_id}... (typically ~1 min)")

    api = AuraAPI(env["CLIENT_ID"], env["CLIENT_SECRET"])
    data = api.create_database(instance_id, database_name)

    database_id = data.get("aura_database_id", data.get("id", "?"))
    print(f"\n=== Database Created ===")
    print(f"Database ID:  {database_id}")
    print(f"Bolt URL:     {data.get('bolt_url', '')}")
    print(f"Status:       {data.get('status', '')}")
    print(f"Instance:     {data.get('instance_id', '?')}")
    print(f"\nConnect with: database=\"{database_id}\"")

    if want_wait:
        api.wait_for_database(instance_id, database_id)

    if want_users:
        _add_users(env, database_id, database_name)

    if dump_file:
        file_path = Path(dump_file)
        if not file_path.exists():
            die(f"Dump file not found: {dump_file}")
        # Set up argv for cmd_upload
        original_argv = sys.argv[:]
        sys.argv = [sys.argv[0], "upload", database_id, dump_file, "--yes"]
        cmd_upload(env)
        sys.argv = original_argv

    return database_id


def cmd_list_dbs(env: dict):
    """List all databases in the multi-db instance with their status."""
    require_env(env, "AURA_INSTANCEID")
    instance_id = env["AURA_INSTANCEID"]

    api = AuraAPI(env["CLIENT_ID"], env["CLIENT_SECRET"])
    databases = api.list_databases(instance_id)

    print(f"Databases in instance {instance_id}:")
    for db in databases:
        database_id = db.get("aura_database_id", db.get("id", "?"))
        detail = api.get_database(instance_id, database_id)
        name = detail.get("name", "")
        status = detail.get("status", "")
        nodes = detail.get("nodes", "")
        rels = detail.get("relationships", "")
        parts = [f"  {database_id}"]
        if name and name != database_id:
            parts.append(name)
        if status:
            parts.append(f"[{status}]")
        if nodes or rels:
            parts.append(f"({nodes} nodes, {rels} rels)")
        print("  ".join(parts))


def _add_users(env: dict, database_id: str, database_name: str = None) -> None:
    """Create read-only and read-write roles and users for a database.

    The ro role gets ACCESS + MATCH. The rw role only adds WRITE.
    The rw user gets both roles, inheriting all ro permissions.
    Writes credential files for each user.
    """
    if database_name is None:
        database_name = database_id
    require_env(env, "NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD")

    ro_user = f"{database_id}_ro"
    rw_user = f"{database_id}_rw"
    ro_role = f"{database_id}_ro"
    rw_role = f"{database_id}_rw"
    ro_pass = _generate_password()
    rw_pass = _generate_password()

    print(f"\nCreating users on {env['NEO4J_URI']} for database '{database_id}'...")
    print(f"  Read-only:  {ro_user}")
    print(f"  Read-write: {rw_user}")
    confirm("")

    query = QueryAPI(env["NEO4J_URI"], env["NEO4J_USERNAME"], env["NEO4J_PASSWORD"])

    print(f"Creating read-only role and user {ro_user}...")
    query.run_many("system", [
        f"CREATE ROLE `{ro_role}`",
        f"GRANT ACCESS ON DATABASE `{database_id}` TO `{ro_role}`",
        f"GRANT MATCH {{*}} ON GRAPH `{database_id}` TO `{ro_role}`",
        f"CREATE USER `{ro_user}` SET PASSWORD '{ro_pass}' SET PASSWORD CHANGE NOT REQUIRED",
        f"GRANT ROLE `{ro_role}` TO `{ro_user}`",
    ])

    print(f"Creating read-write role and user {rw_user}...")
    query.run_many("system", [
        f"CREATE ROLE `{rw_role}`",
        f"GRANT WRITE ON GRAPH `{database_id}` TO `{rw_role}`",
        f"CREATE USER `{rw_user}` SET PASSWORD '{rw_pass}' SET PASSWORD CHANGE NOT REQUIRED",
        f"GRANT ROLE `{ro_role}` TO `{rw_user}`",
        f"GRANT ROLE `{rw_role}` TO `{rw_user}`",
    ])

    print("\nVerifying...")
    query.run_and_print("system", "SHOW USERS")

    # Write credential files
    instance_id = env.get("AURA_INSTANCEID", "unknown")
    instance_name = env.get("AURA_INSTANCENAME", "")
    bolt_uri = env["NEO4J_URI"]
    script_dir = Path(__file__).parent

    credentials = [(ro_user, ro_pass, "readonly"), (rw_user, rw_pass, "readwrite")]
    print(f"\n=== Users Created ===")
    for user, password, role_label in credentials:
        name_part = f"-{database_name}" if database_name != database_id else ""
        filename = f"Neo4j-{instance_id}-{database_id}{name_part}-{role_label}.txt"
        filepath = script_dir / filename
        _write_credential_file(filepath,
            f"# Neo4j Aura — {role_label} credentials for database {database_id}\n"
            f"# Instance: {instance_id} ({instance_name})\n"
            f"NEO4J_URI={bolt_uri}\n"
            f"NEO4J_USERNAME={user}\n"
            f"NEO4J_PASSWORD={password}\n"
            f"NEO4J_DATABASE={database_id}\n"
        )
        print(f"  {role_label}: {user} -> {filepath.name}")


def cmd_add_users(env: dict):
    """CLI wrapper for _add_users — parses args and delegates."""
    args = positional_args()
    database_id = get_arg(2, "add-users <dbid> [dbname]")
    database_name = args[3] if len(args) > 3 else database_id
    _add_users(env, database_id, database_name)


def cmd_upload(env: dict):
    """Upload a dump or backup file to a database via the console import API.

    Steps: authenticate -> size check -> initiate presigned upload ->
    GCP resumable upload -> signal complete -> poll until imported.
    """
    args = positional_args()
    if len(args) < 4:
        die("Usage: upload <dbname> <dump-file|backup-file>")
    database_id = args[2]
    file_path = Path(args[3])
    require_env(env, "NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD")

    if not file_path.exists():
        die(f"File not found: {file_path}")

    console_base = _console_base_url(env["NEO4J_URI"])
    file_size = file_path.stat().st_size

    print(f"Uploading to database '{database_id}' (typically ~3 min)")
    print(f"  File:     {file_path.name} ({file_size / (1024*1024):.1f} MB)")
    print(f"  Target:   {console_base}")
    confirm("")

    print("Computing CRC32 checksum...")
    crc32 = _crc32_checksum(file_path)
    print(f"  CRC32: {crc32}")

    importer = ConsoleImporter(console_base, database_id,
                               env["NEO4J_USERNAME"], env["NEO4J_PASSWORD"])
    importer.authenticate()
    importer.check_size(file_size)

    provider, signed_uri = importer.initiate_upload(crc32, file_size)
    if provider.upper() != "GCP":
        die(f"Only GCP upload is supported, got provider: {provider}")

    _gcp_resumable_upload(signed_uri, file_path, file_size)

    importer.signal_complete(crc32)
    importer.poll_import_status()

    # Verify upload with a node/relationship count
    print("Verifying upload...")
    query = QueryAPI(env["NEO4J_URI"], env["NEO4J_USERNAME"], env["NEO4J_PASSWORD"])
    query.run_and_print(database_id,
        "MATCH (n) WITH count(n) AS nodes "
        "OPTIONAL MATCH ()-[r]->() "
        "RETURN nodes, count(r) AS relationships")



def _remove_users_and_roles(env: dict, database_id: str) -> None:
    """Drop the ro/rw users and roles for a database."""
    require_env(env, "NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD")
    ro_name = f"{database_id}_ro"
    rw_name = f"{database_id}_rw"

    print(f"Removing users/roles for database '{database_id}' ({ro_name}, {rw_name})...")
    query = QueryAPI(env["NEO4J_URI"], env["NEO4J_USERNAME"], env["NEO4J_PASSWORD"])
    query.run_many("system", [
        f"DROP USER `{ro_name}` IF EXISTS",
        f"DROP USER `{rw_name}` IF EXISTS",
        f"DROP ROLE `{ro_name}` IF EXISTS",
        f"DROP ROLE `{rw_name}` IF EXISTS",
    ])
    print("  Users/roles removed.")


def cmd_delete_db(env: dict):
    """Delete a database and its associated users/roles.

    Use --wait to poll until the database is fully removed.
    """
    database_id = get_arg(2, "delete-db <dbid> [--wait]")
    require_env(env, "AURA_INSTANCEID")
    instance_id = env["AURA_INSTANCEID"]

    confirm(f"Deleting database '{database_id}' from instance {instance_id}... (typically ~1 min)")

    _remove_users_and_roles(env, database_id)

    api = AuraAPI(env["CLIENT_ID"], env["CLIENT_SECRET"])
    data = api.delete_database(instance_id, database_id)
    print(f"Database '{database_id}' status: {data.get('status', 'deleted')}")

    if has_flag("--wait"):
        api.wait_for_database_gone(instance_id, database_id)


def cmd_remove_users(env: dict):
    """Remove users and roles associated with a database (without deleting the database)."""
    database_id = get_arg(2, "remove-users <dbid>")
    confirm(f"Remove users/roles for database '{database_id}'?")
    _remove_users_and_roles(env, database_id)


# --- Main ---

COMMANDS = {
    "create-instance": cmd_create_instance,
    "add-db": cmd_add_db,
    "list-dbs": cmd_list_dbs,
    "add-users": cmd_add_users,
    "upload": cmd_upload,
    "delete-db": cmd_delete_db,
    "remove-users": cmd_remove_users,
}


def _parse_creds_flag() -> str:
    """Extract the --creds <file> value from sys.argv, or return None."""
    return _parse_flag_value("--creds")


def main():
    """Parse command and dispatch to the appropriate handler."""
    args = positional_args()
    if len(args) < 2 or args[1] not in COMMANDS:
        usage()

    # Load env: os.environ <- .env <- --creds file (highest priority)
    script_dir = Path(__file__).parent
    env = {**os.environ, **load_env(script_dir / ".env")}

    creds_file = _parse_creds_flag()
    if creds_file:
        creds_path = Path(creds_file)
        if not creds_path.exists():
            die(f"Credentials file not found: {creds_file}")
        env.update(load_env(creds_path))
        print(f"Loaded credentials from {creds_path.name}")

    command = args[1]
    if command != "create-instance":
        require_env(env, "CLIENT_ID", "CLIENT_SECRET", "PROJECT_ID")

    start = time.time()
    COMMANDS[command](env)
    elapsed = time.time() - start
    if elapsed >= 60:
        print(f"\nCompleted in {elapsed / 60:.1f} minutes.")
    elif elapsed >= 1:
        print(f"\nCompleted in {elapsed:.1f} seconds.")


if __name__ == "__main__":
    main()
