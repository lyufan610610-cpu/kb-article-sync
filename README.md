# KB-Article-Sync: Enterprise CMS-to-Dify Knowledge Base Synchronization Gateway

[English](README.md) | [中文](README_zh.md)

A decoupled, production-grade synchronization gateway that bridges enterprise Content Management Systems (CMS, OA, wikis) with **Dify Knowledge Base (RAG)**. It automates file ingestion, provisions dual-purpose metadata (`article_id` for RBAC permission checks, `source_url` for answer citation backlinks), and manages document lifecycles through hierarchical parent-child chunking.

---

## Architecture Overview

In enterprise RAG deployments, synchronizing external knowledge into Dify requires more than simple file uploads:
1. **Access Control (RBAC)**: Search and retrieval pipelines must verify employee read permissions against enterprise article IDs (`article_id`) after retrieval.
2. **Citation Source Linking**: AI assistant responses need direct hyperlinks (`source_url`) pointing back to the original portal article.
3. **Decoupled Maintenance**: Modifying Dify source code directly introduces severe maintenance debt during upstream updates.
4. **Idempotency & Cost Control**: Uncontrolled re-indexing burns expensive vectorization credits and embedding model tokens.

`kb-article-sync` operates as an independent synchronization gateway deployed alongside Dify. It interacts exclusively via Dify's public Service REST API, ensuring zero coupling with Dify's internal codebase.

```
+----------------------------------------------------------------------------------------------------+
|                                    Enterprise Architecture Overview                                |
|                                                                                                    |
|  +--------------------+                                                                            |
|  | Enterprise CMS /   |                                                                            |
|  | Knowledge Systems  |                                                                            |
|  +--------------------+                                                                            |
|            |                                                                                       |
|            | 1. Batch / Single Push (JSON / Multipart)                                             |
|            v                                                                                       |
|  +----------------------------------------------------------------------------------------------+  |
|  |                                  kb-article-sync Gateway                                     |  |
|  |                                                                                              |  |
|  |  +------------------------+  +-------------------------+  +-------------------------------+  |  |
|  |  | Token Auth & Isolation |  | Timestamp Cache Check   |  | Hierarchical Chunk Formatter  |  |  |
|  |  | (X-KB-Sync-Token)      |  | (SQLite mapping.db)     |  | (1024 Parent / 200 Child)     |  |  |
|  |  +------------------------+  +-------------------------+  +-------------------------------+  |  |
|  |              |                            |                               |                  |  |
|  |              v                            v                               v                  |  |
|  |  +----------------------------------------------------------------------------------------+  |  |
|  |  | Dynamic Metadata Binding (source_url, article_id) & In-Place / Recreate State Machine  |  |  |
|  |  +----------------------------------------------------------------------------------------+  |  |
|  +----------------------------------------------------------------------------------------------+  |
|            |                                                                                       |
|            | 2. Standard Service REST API Calls (HTTP)                                             |
|            v                                                                                       |
|  +----------------------------------------------------------------------------------------------+  |
|  |                                       Dify Service API                                       |  |
|  |                                                                                              |  |
|  |  +------------------------------+     +---------------------------------------------------+  |  |
|  |  | Dataset Custom Metadata API |     | Document Ingestion & Vector Indexing Pipeline     |  |  |
|  |  | (article_id, source_url)     |     | (Hierarchical Model: High Precision Search)       |  |  |
|  |  +------------------------------+     +---------------------------------------------------+  |  |
|  +----------------------------------------------------------------------------------------------+  |
+----------------------------------------------------------------------------------------------------+
```

---

## Key Capabilities

- **Zero-Intrusion Decoupled Architecture**: Communicates strictly via standard Dify Service APIs. Upgrades, rebases, or docker restarts on Dify require zero changes to this service.
- **Dual-Purpose Metadata Backfilling**: Dynamically creates and injects custom metadata attributes into Dify datasets:
  - `article_id`: Enables post-retrieval permission checking (RBAC/ACL) against the corporate identity system before surfacing answers to users.
  - `source_url`: Provides one-click original document backlinks in the assistant's citation footers.
- **Hierarchical (Parent-Child) Chunking**: Configures two-tier semantic chunking by default (`doc_form="hierarchical_model"`):
  - **Parent Chunks (1024 tokens)**: Injected into the LLM context window to provide rich surrounding paragraph context.
  - **Child Sub-chunks (200 tokens)**: High-resolution units indexed in the vector database for pinpoint similarity retrieval.
- **Idempotency & Change Detection**: Tracks publication timestamps (`updated_at`) in a local SQLite store (`mapping.db`). Unchanged articles are skipped automatically, preventing redundant embedding costs.
- **Self-Healing Update Lifecycle**: Attempts native in-place updates (`update-by-file`). If Dify rejects the update with HTTP 400 (e.g. document still indexing, archived, or disabled), the service automatically falls back to an atomic delete-and-recreate sequence.
- **Fault-Tolerant Partial Processing**: Batch endpoints validate entries individually. Corrupted items return structured failure details without blocking valid items in the same payload.
- **Safety-Guarded Full Reconciliation (`full_sync: true`)**: Prunes unlisted stale documents only when the incoming batch has zero validation errors, eliminating the risk of accidental bulk data deletion caused by corrupt inputs.

---

## Directory Structure

```
kb-article-sync/
├── post.py            # Core Flask sync service & state machine
├── env.example.sh     # Configuration template (tokens, endpoints, dataset ID)
├── requirements.txt   # Minimal Python dependencies (flask, requests)
├── tmux.sh            # Non-root daemon supervisor (start/stop/restart/logs/status)
├── start.sh           # Shortcut to ./tmux.sh start
├── stop.sh            # Shortcut to ./tmux.sh stop
├── run.sh             # Foreground debugging launcher
├── selftest.sh        # Automated end-to-end integration test suite
├── mapping.db         # Local SQLite state mapping (runtime-generated, gitignored)
└── logs/app.log       # Application execution logs (runtime-generated, gitignored)
```

---

## Quickstart

### 1. Installation
Deploy to your application server (e.g., `/opt/kb-sync`):

```bash
cd /opt/kb-sync
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 2. Configure Environment
Copy `env.example.sh` to `env.sh` and populate your credentials:

```bash
cp env.example.sh env.sh
chmod 600 env.sh
vi env.sh
```

### 3. Start the Daemon & Verify
```bash
# Start background service
./tmux.sh start

# Check service status
./tmux.sh status

# Run the comprehensive integration test suite
./selftest.sh
```

---

## Configuration Reference

| Environment Variable | Default Value | Description |
|---|---|---|
| `DIFY_API_KEY` | `""` | Dify Knowledge Base Service API Key (`app-...`). |
| `KB_SYNC_TOKEN` | `""` | Shared authentication secret required in `X-KB-Sync-Token` request headers. |
| `DIFY_DATASET_ID` | `00000000-...` | Target Dify Knowledge Base dataset UUID. |
| `DIFY_BASE` | `http://127.0.0.1:8080/v1` | Dify API base endpoint. |
| `PORT` | `8090` | Port on which the synchronization service listens. |
| `META_FIELDS` | `source_url,article_id` | Metadata fields to provision and write into Dify documents. |
| `MAPPING_DB` | `./mapping.db` | Path to the local SQLite state store. |

---

## API Reference

All requests must include the header:
```http
X-KB-Sync-Token: <KB_SYNC_TOKEN>
```

### 1. Batch Article Push
- **Path**: `POST /sync/kb/article/push`
- **Content-Type**: `application/json`
- **Batch Limit**: 200 items per request

#### Request Body
```json
{
  "full_sync": false,
  "items": [
    {
      "article_id": 1001,
      "title": "Engineering Standards and Best Practices",
      "file_name": "engineering_standards.docx",
      "file_url": "https://cms.internal.example.com/files/1001.docx",
      "source_url": "https://cms.internal.example.com/article/1001",
      "publish_status": "published",
      "updated_at": "2026-10-20T10:00:00+08:00",
      "category": "R&D / Standards",
      "author": "Tech Committee",
      "secret_level": "Internal"
    }
  ]
}
```

#### Response (`200 OK`)
```json
{
  "code": 200,
  "status": "success",
  "msg": "Batch processing completed",
  "data": {
    "accepted": 1,
    "rejected": 0,
    "offline": 0,
    "errors": []
  }
}
```

If individual items fail validation, the endpoint returns HTTP 200 with partial status:
```json
{
  "code": 200,
  "status": "partial",
  "msg": "Some items failed validation",
  "data": {
    "accepted": 1,
    "rejected": 1,
    "offline": 0,
    "errors": [
      {
        "index": 1,
        "article_id": 1002,
        "field": "file_name",
        "reason": "file_name cannot be empty"
      }
    ]
  }
}
```

---

### 2. Single Article Push (Direct File Upload)
- **Path**: `POST /api/sync_article`
- **Content-Type**: `multipart/form-data`

```bash
curl -F "article_id=1001" \
     -F "title=Engineering Standards" \
     -F "source_url=https://cms.internal.example.com/article/1001" \
     -F "publish_status=published" \
     -F "file=@/path/to/standards.docx" \
     -H "X-KB-Sync-Token: $KB_SYNC_TOKEN" \
     http://127.0.0.1:8090/api/sync_article
```

---

### 3. Metadata Reverse Lookup
- **Path**: `GET /api/article_meta?article_id=1001`

Returns local mapping details including `document_id`, `source_url`, and online status:
```json
{
  "code": 200,
  "data": {
    "article_id": 1001,
    "document_id": "4e7235a1-7734-4b5c-b17f-4c12690184b2",
    "doc_name": "Engineering Standards",
    "source_url": "https://cms.internal.example.com/article/1001",
    "online": true
  }
}
```

---

### 4. Health & Status Check
- **Path**: `GET /healthz`

Returns online article counts for monitoring and reconciliation:
```json
{
  "status": "ok",
  "online_articles": 1420
}
```

---

## Operations & Process Management

The service includes `tmux.sh`, a dedicated process manager designed for enterprise server environments without requiring `root` or `sudo` privileges:

```bash
./tmux.sh start      # Starts service daemon and verifies /healthz
./tmux.sh status     # Checks daemon running status
./tmux.sh logs       # Tails real-time logs (Ctrl+C to exit viewing, process keeps running)
./tmux.sh attach     # Attaches to interactive console session (Detach with Ctrl+B, then D)
./tmux.sh restart    # Restarts daemon with fresh configuration
./tmux.sh stop       # Gracefully terminates the daemon session
```

---

## Automated Verification (`selftest.sh`)

Run `./selftest.sh` to execute the full automated test suite against a live or staging Dify instance:

```bash
./selftest.sh
```

**Tested Assertions**:
1. Unauthorized request rejection (HTTP 403 on missing or invalid token).
2. Mock file server provisioning.
3. First-time document creation and ingestion.
4. Idempotency verification (identical `updated_at` triggers immediate skip without re-indexing).
5. Document content update propagation.
6. Validation fault-isolation (corrupt record rejection does not block valid record).
7. Metadata reverse lookup via `/api/article_meta`.
8. Remote Dify API verification: confirms both `source_url` and `article_id` metadata are persisted.
9. Unpublish lifecycle (unpublishing cleanly removes the document from Dify).
10. Safe full reconciliation test and automated test data cleanup.

---

## Security Best Practices

- **File Permissions**: Restrict `env.sh` permissions to `chmod 600 env.sh`. Never commit production credentials to version control.
- **Token Entropy**: Set `KB_SYNC_TOKEN` to a cryptographically secure random string (e.g. `openssl rand -hex 32`).
- **Database Backup**: Regularly back up `mapping.db` to protect synchronization state history.
