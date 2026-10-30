# KB-Article-Sync: Dual-Library Enterprise Knowledge Synchronization Gateway & Chunking Engine

[English](README.md) | [中文](README_zh.md)

A decoupled, production-grade enterprise synchronization gateway bridging corporate Content Management Systems (CMS, OA, wikis) with **Dify Knowledge Base (RAG)**. 

Beyond standard document ingestion, it provides **dual-library intelligent routing** (R&D Knowledge Base vs. QA Document Base), a **constant-time $O(1)$ AI document classifier**, **two-layer dynamic chunking rules governance** for text/markdown streams, and an **extended self-healing state machine** supporting cross-library migrations and UI deletion recovery.

---

## Architecture Overview

In production RAG systems, syncing enterprise knowledge into Dify requires solving several structural challenges:
1. **Heterogeneous Document Routing**: General technical documentation and structured Q&A manuals require completely different chunking topologies and target knowledge bases.
2. **Text Chunking Fragmentation**: Raw text (`.txt`) and Markdown with irregular line breaks get chopped into fragmented micro-chunks by standard `\n\n` parent-child rules, breaking semantic retrieval.
3. **Delimiter Stripping Invariant**: Dify's `FixedRecursiveCharacterTextSplitter` discards delimiters during splitting; using naive delimiters like `\n\nQ:` strips the `Q:` prefix from every chunk heading.
4. **Access Control (RBAC) & Traceability**: Search pipelines must verify employee permissions against enterprise article IDs (`article_id`) and backlink answers to source URLs (`source_url`).
5. **State Self-Healing & Decoupled Maintenance**: Changes made in the Dify UI (manual document deletions) or pending indexing states must not break synchronization.

`kb-article-sync` runs as an independent gateway communicating with Dify exclusively through standard Service REST APIs, ensuring zero coupling with Dify's internal code.

```
+----------------------------------------------------------------------------------------------------+
|                                    Enterprise Architecture Overview                                |
|                                                                                                    |
|  +--------------------+                                                                            |
|  | Enterprise CMS /   |                                                                            |
|  | Knowledge Systems  |                                                                            |
|  +--------------------+                                                                            |
|            |                                                                                       |
|            | 1. Batch / Single Push (JSON / Multipart Form)                                         |
|            v                                                                                       |
|  +----------------------------------------------------------------------------------------------+  |
|  |                                  kb-article-sync Gateway                                     |  |
|  |                                                                                              |  |
|  |  +------------------------+  +--------------------------+  +------------------------------+  |  |
|  |  | Request Audit Logger   |  | Token Auth & Isolation   |  | Timestamp Cache Check        |  |  |
|  |  | (@app.before_request)  |  | (X-KB-Sync-Token)        |  | (SQLite mapping.db)          |  |  |
|  |  +------------------------+  +--------------------------+  +------------------------------+  |  |
|  |                                           |                                                  |  |
|  |                                           v                                                  |  |
|  |  +----------------------------------------------------------------------------------------+  |  |
|  |  | Constant-Time O(1) AI Document Classifier (Local LLM: DeepSeek-R1-14B)                 |  |  |
|  |  | Extracts first ~3,000 chars from DOCX, PDF, TXT, MD, HTML -> doc_type=1 (RD) / 2 (QA)  |  |  |
|  |  +----------------------------------------------------------------------------------------+  |  |
|  |                                           |                                                  |  |
|  |                                           v                                                  |  |
|  |  +----------------------------------------------------------------------------------------+  |  |
|  |  | Two-Layer Dynamic Chunking Rules Governance                                             |  |  |
|  |  | - Layer 1: Extension filter (only .txt / .md enter deep shape analysis)                  |  |  |
|  |  | - Layer 2: Shape triage (QA @@QA@@ 1024/1024 | Prose @@CHUNK@@ 1024/300 | List default) |  |  |
|  |  +----------------------------------------------------------------------------------------+  |  |
|  |                                           |                                                  |  |
|  |                                           v                                                  |  |
|  |  +----------------------------------------------------------------------------------------+  |  |
|  |  | Self-Healing State Machine: In-Place Update -> 400 Recreate -> 404 UI-Deleted Recreate |  |  |
|  |  | Cross-Library Migration -> Metadata Binding (source_url, article_id)                   |  |  |
|  |  +----------------------------------------------------------------------------------------+  |  |
|  +----------------------------------------------------------------------------------------------+  |
|            |                                                                                       |
|            | 2. Standard Service REST API Calls (HTTP)                                             |
|            +-----------------------------------+                                                   |
|            |                                   |                                                   |
|            v                                   v                                                   |
|  +-----------------------------------+   +---------------------------------------------------+     |
|  | R&D Knowledge Base (DATASET_RD)   |   | QA Document Base (DATASET_QA)                     |     |
|  | doc_type = 1                      |   | doc_type = 2                                      |     |
|  | Prose & Structured Specifications |   | 1 Q&A Pair = 1 Parent = 1 Child (Preserved Q:)    |     |
|  +-----------------------------------+   +---------------------------------------------------+     |
+----------------------------------------------------------------------------------------------------+
```

---

## Core Technical Innovations

### 1. Dual-Library Intelligent Routing
The gateway routes incoming articles to one of two specialized Dify knowledge bases:
- **R&D Knowledge Base (`DATASET_RD`, `doc_type=1`)**: For general technical documentation, engineering standards, architecture designs, and release notes.
- **QA Document Base (`DATASET_QA`, `doc_type=2`)**: Dedicated to Q&A handbooks and FAQ manuals, optimized for high-precision semantic matching.

### 2. Constant-Time $O(1)$ AI Document Classifier
- **Lightweight Sample Extraction**: Extracts text strictly from the document head (first ~3,000 characters, or first 3 pages of a PDF) directly from memory (`.docx` via `word/document.xml`, `.pdf` via streaming extractors, `.txt`/`.md`/`.html` via clean tag stripping).
- **Constant Latency**: Classification latency is fixed and independent of document size. A 100 MB manual evaluates just as fast as a 5 KB note.
- **Fail-Open Non-Blocking Design**: If the local classifier model (e.g. DeepSeek-R1-14B) times out or encounters network issues, the gateway automatically falls back to `doc_type=1` (R&D Knowledge Base). Ingestion is never blocked.
- **Deterministic Override**: Callers can pass an explicit `doc_type` parameter to bypass model classification.

### 3. Two-Layer Dynamic Chunking Rules Governance
Dify's global dataset rules cannot satisfy both rich documents (PDF/PPTX) and raw text streams. Modifying global separators breaks child chunk precision for other files. The gateway solves this via document-level `process_rule` injection:

```
[Uploaded Document]
        |
        v
[Layer 1: Extension Filter]
        |-- Binary / Rich formats (.pdf, .docx, .pptx, .xlsx) ------> Retain Default Library Rules
        |
        v Text streams (.txt, .md, .markdown, .mdx)
[Layer 2: Content Shape Analysis (First 20,000 chars)]
        |
        |-- 1. "qa" Shape: Lines with Q:/问: >= 5 and >= 15% non-empty lines
        |      => Injects delimiter @@QA@@ before each Q&A pair
        |      => Splitter rules: parent 1024 / child 1024 (1 Q&A pair = 1 parent = 1 child)
        |      => Invariant: Prevents Dify from discarding "Q:" during splitting!
        |
        |-- 2. "prose" Shape: Narrative text with hard line breaks & section titles
        |      => Normalization: Merges broken lines not ending with terminal punctuation
        |      => Headings isolated into standalone paragraphs; inter-paragraph unified to \n\n
        |      => Splitter rules: parent 1024 / child 300 using @@CHUNK@@ marker
        |
        +-- 3. "list" Shape: Catalogs, parameter listings, table dumps
               => Retain Default Library Rules
```

> **Deep Dive**: For full experimental benchmarks, test cases, and design trade-offs, see [`docs/2026-10-30-dify-chunking-two-layer-rules.md`](./docs/2026-10-30-dify-chunking-two-layer-rules.md).

### 4. Extended Self-Healing State Machine
- **HTTP 400 (`Document is not available`)**: Occurs when Dify rejects in-place updates because the previous indexing failed or document is in a pending state. Automatically falls back to atomic deletion and re-creation.
- **HTTP 404 (`Document not found`)**: Occurs when a document was manually deleted via the Dify Web UI while `mapping.db` still holds the obsolete `document_id`. The gateway catches 404, triggers a fresh creation, backfills the new `document_id` into SQLite, and returns `status="created"` instead of misreporting an update.
- **Cross-Library Migration**: If an article's `doc_type` transitions (e.g. an R&D document becomes a Q&A manual), the gateway purges the record from the old dataset and establishes it cleanly in the new dataset.
- **Zero-Error Guarded Reconciliation (`full_sync: true`)**: Stale online documents not included in the batch are purged *only if* the incoming batch has zero validation errors, preventing accidental wipeout on corrupted payloads.

---

## Directory Structure

```
kb-article-sync/
├── post.py            # Core Flask sync gateway, classifier & state machine
├── env.example.sh     # Environment configuration template (dual datasets, classifier)
├── requirements.txt   # Minimal dependencies (flask, requests)
├── tmux.sh            # Non-root daemon supervisor (start/stop/restart/logs/status)
├── start.sh           # Shortcut: ./tmux.sh start
├── stop.sh            # Shortcut: ./tmux.sh stop
├── run.sh             # Foreground debugging launcher
├── selftest.sh        # Automated end-to-end integration test suite
├── docs/              # Architectural governance & design records
│   └── 2026-10-30-dify-chunking-two-layer-rules.md
├── mapping.db         # Local SQLite state store (runtime-generated, gitignored)
└── logs/app.log       # Application logs (runtime-generated, gitignored)
```

---

## Configuration Reference

Configure your settings in `env.sh` (copied from `env.example.sh`):

```bash
# ----------------- Dify Core Settings -----------------
DIFY_API_KEY=dataset-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
KB_SYNC_TOKEN=change-me-to-a-long-random-string
DIFY_BASE=http://127.0.0.1:8082/v1
PORT=8090

# Dual Target Knowledge Bases
DIFY_DATASET_ID=3e5855a3-f2a5-4b96-a05d-eff18406f66a       # R&D Knowledge Base (doc_type=1)
DIFY_DATASET_QA_ID=d5582337-85ed-43a3-abc2-aa9b1c24913b    # QA Document Base (doc_type=2)

# ----------------- AI Document Classifier -----------------
CLASSIFIER_BASE=http://127.0.0.1:8001/v1
CLASSIFIER_MODEL=deepseek-r1-14b-awq
CLASSIFIER_API_KEY=
CLASSIFIER_TIMEOUT=90
CLASSIFIER_CHARS=3000
CLASSIFIER_PDF_PAGES=3

# ----------------- Metadata & State -----------------
META_FIELDS=source_url,article_id
MAPPING_DB=./mapping.db
```

### Parameter Details

| Variable | Default Value | Description |
|---|---|---|
| `DIFY_DATASET_ID` | `""` | Target dataset UUID for standard R&D documentation (`doc_type=1`). |
| `DIFY_DATASET_QA_ID` | `""` | Target dataset UUID for Q&A manuals and FAQs (`doc_type=2`). |
| `CLASSIFIER_BASE` | `http://127.0.0.1:8001/v1` | OpenAI-compatible endpoint for local classification LLM. |
| `CLASSIFIER_MODEL` | `deepseek-r1-14b-awq` | Model identifier for fast document classification. |
| `CLASSIFIER_CHARS` | `3000` | Character count sampled from document head for classification. |
| `CLASSIFIER_PDF_PAGES` | `3` | Maximum number of PDF pages to extract during classification. |
| `META_FIELDS` | `source_url,article_id` | Document metadata attributes provisioned and backfilled into Dify. |

---

## API Reference

All requests must include the authentication header:
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
      "title": "Engineering Standards & Best Practices",
      "file_name": "engineering_standards.docx",
      "file_url": "https://cms.internal.example.com/files/1001.docx",
      "source_url": "https://cms.internal.example.com/article/1001",
      "publish_status": "published",
      "updated_at": "2026-10-30T10:00:00+08:00",
      "category": "Architecture",
      "author": "Tech Committee",
      "secret_level": "Internal",
      "doc_type": 1
    }
  ]
}
```

> **Note**: `doc_type` is optional (`1` = R&D Knowledge Base, `2` = QA Document Base). If omitted, the AI classifier evaluates the document excerpt automatically.

#### Partial Success Response (`200 OK`)
If certain records fail validation, they are isolated without failing the rest of the batch:
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
     -F "title=Z5 Parameters FAQ" \
     -F "source_url=https://cms.internal.example.com/article/1001" \
     -F "publish_status=published" \
     -F "doc_type=2" \
     -F "file=@/path/to/faq.txt" \
     -H "X-KB-Sync-Token: $KB_SYNC_TOKEN" \
     http://127.0.0.1:8090/api/sync_article
```

---

### 3. Metadata Reverse Lookup
- **Path**: `GET /api/article_meta?article_id=1001`

Returns local mapping details, current library assignment, and online status:
```json
{
  "code": 200,
  "data": {
    "article_id": 1001,
    "document_id": "4e7235a1-7734-4b5c-b17f-4c12690184b2",
    "doc_name": "Z5 Parameters FAQ",
    "source_url": "https://cms.internal.example.com/article/1001",
    "online": true,
    "dataset_id": "d5582337-85ed-43a3-abc2-aa9b1c24913b",
    "doc_type": 2,
    "library": "QA文档库"
  }
}
```

---

### 4. Health & Status Check
- **Path**: `GET /healthz`

Provides an operational health report, classifier configuration, and online article counts broken down by library:
```json
{
  "status": "ok",
  "dataset_id": "3e5855a3-f2a5-4b96-a05d-eff18406f66a",
  "dataset_qa_id": "d5582337-85ed-43a3-abc2-aa9b1c24913b",
  "dify_base": "http://127.0.0.1:8082/v1",
  "key_loaded": true,
  "classifier": {
    "base": "http://127.0.0.1:8001/v1",
    "model": "deepseek-r1-14b-awq"
  },
  "online_articles": 1540,
  "online_by_library": {
    "研发知识库": 1280,
    "QA文档库": 260
  }
}
```

---

## Operations & Verification

```bash
# Process Management (tmux daemon)
./tmux.sh start      # Starts gateway and checks /healthz
./tmux.sh status     # Checks daemon running status
./tmux.sh logs       # Tails real-time logs (Ctrl+C exits viewer, service keeps running)
./tmux.sh restart    # Restarts service with updated configuration
./tmux.sh stop       # Gracefully shuts down service

# Run automated integration test suite
./selftest.sh
```

---

## Security Best Practices

- **File Permissions**: Keep `env.sh` locked to `chmod 600 env.sh`. Never commit production secrets.
- **Token Entropy**: Set `KB_SYNC_TOKEN` to a cryptographically strong string (e.g. `openssl rand -hex 32`).
- **Database Backup**: Regularly back up `mapping.db` to protect document state mapping history.
