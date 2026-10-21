"""
知识库文章同步接口  (研发知识库 / Dify)
=======================================
部署位置 : 与 Dify 源码目录同级，例如 /opt/kb-sync
重要说明 : 本文件与 Dify 源码目录完全隔离，
           不 import 任何 Dify 代码，只通过 HTTP 调用 Dify 的 Service API。
           因此 pull / rebase / 二次开发 Dify 源码都不会影响本服务。

启动方式 :
    cd /opt/kb-sync
    ./run.sh                    # 前台（Ctrl+C 停）
    ./start.sh                  # 后台，跑在 tmux 会话里（ssh 断开也不受影响）
    ./stop.sh                   # 停止
    ./selftest.sh               # 自测，跑完自动清理

对外接口 :
    POST /sync/kb/article/push  JSON 批量推送（对接文档里的正式形态）
    POST /api/sync_article      multipart 单条推送（文件直接带过来时用）
    GET  /api/article_meta      按 article_id 反查 document_id / 源文件 url
    GET  /healthz               健康检查
"""

import io
import json
import logging
import mimetypes
import os
import re
import sqlite3
import threading

import requests
from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("kb-sync")

app = Flask(__name__)

# ---------------------------------------------------------------- 配置
DATASET_ID = os.environ.get("DIFY_DATASET_ID", "00000000-0000-0000-0000-000000000000")
# 本服务与 Dify 同机部署，直接走宿主机的 127.0.0.1:8080 即可
DIFY_BASE = os.environ.get("DIFY_BASE", "http://127.0.0.1:8080/v1")
# 环境变量里只放裸 key，Bearer 前缀在代码里拼，避免两处各写一半
DIFY_API_KEY = os.environ.get("DIFY_API_KEY", "")
SYNC_TOKEN = os.environ.get("KB_SYNC_TOKEN", "")
DB_PATH = os.environ.get(
    "MAPPING_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "mapping.db")
)
# 这些业务字段会额外写进 Dify 文档元数据，供检索后鉴权比对、以及答案回链原文用。
# article_id 是关键项：鉴权接口返回的就是这个正整数文章编号。
META_FIELDS = [
    f.strip() for f in os.environ.get("META_FIELDS", "source_url,article_id").split(",")
    if f.strip()
]

# 与 Dify 的 DOCUMENT_EXTENSIONS 对齐（ETL_TYPE=dify 走的那份内置清单）。
# Dify 侧以后收紧或放开格式，这里要同步改，否则会出现「脚本放行、Dify 拒收」的 400。
ALLOWED_EXT = {
    ".txt", ".markdown", ".md", ".mdx",
    ".pdf",
    ".html", ".htm",
    ".xlsx", ".xls",
    ".docx", ".csv",
    ".vtt", ".properties", ".odt",
    ".pptx",
}
MAX_BATCH = 200
DIFY_TIMEOUT = 120
DOWNLOAD_TIMEOUT = 120
# 单文件体积上限，必须和 Dify 的 UPLOAD_FILE_SIZE_LIMIT 对齐：
# Dify 默认就是 15（MB），超过会在 Dify 那层被拒，而且它的报错信息很不友好，
# 所以在这里先挡一道，让对方早点拿到清楚的提示。Dify 调大这里也要跟着调。
MAX_FILE_BYTES = 15 * 1024 * 1024  # 15MB，对应 Dify 的 UPLOAD_FILE_SIZE_LIMIT=15

# ---------------------------------------------------------------- 切分策略
# 关键：Dify 选哪个分段器看的是顶层 doc_form，不是 process_rule.mode。
# doc_form 白名单只有 text_model / hierarchical_model / qa_model（见
# services/entities/knowledge_entities.py）；不传的话 Dify 会兜底成
# dataset.chunk_structure or "text_model"，而我们建库时没设 chunk_structure，
# 于是静默落回普通段落切分 —— 下面这套父子参数会全部作废。
PROCESS_RULE = {
    "indexing_technique": "high_quality",
    "doc_form": "hierarchical_model",
    "process_rule": {
        "mode": "hierarchical",
        "rules": {
            "pre_processing_rules": [
                {"id": "remove_extra_spaces", "enabled": True},
                {"id": "remove_urls_emails", "enabled": False},
            ],
            # 父块：只作为上下文回带给模型，不进向量库，所以给大一些（约一个小节）。
            # paragraph = 每个父块独立成段；full-doc = 整篇当一个父块，长技术文不适用。
            # 注意这个字段必须给，缺省是 None，Dify 两个分支都进不去，会静默切出 0 段。
            "parent_mode": "paragraph",
            "segmentation": {
                "separator": "\n\n",
                "max_tokens": 1024,
            },
            # 子块：真正被向量化、被检索命中的单元，必须小，命中才准。
            # 缺失时 Dify 直接抛 No subchunk segmentation found in rules.
            # chunk_overlap 不给 == 0，和 Dify 前端父子模式的行为一致（它也不发这个字段）。
            "subchunk_segmentation": {
                "separator": "\n",
                "max_tokens": 200,
            },
        },
    },
}


class ItemError(Exception):
    """单条数据不合格，只退这一条。"""

    def __init__(self, field, reason):
        super().__init__(reason)
        self.field = field
        self.reason = reason


# ---------------------------------------------------------------- 存储
def connect():
    # timeout 让并发写等一下锁，而不是直接报 database is locked
    return sqlite3.connect(DB_PATH, timeout=10)


def init_db():
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kb_document_mapping (
                document_id TEXT PRIMARY KEY,
                dataset_id  TEXT NOT NULL,
                article_id  INTEGER NOT NULL,
                doc_name    TEXT,
                src_file    TEXT,
                source_url  TEXT,
                src_updated_at TEXT,
                category    TEXT,
                author      TEXT,
                secret_level TEXT,
                status      INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (dataset_id, article_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_map_article "
            "ON kb_document_mapping(article_id)"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS kb_config (k TEXT PRIMARY KEY, v TEXT)")

        # 老库升级：缺列就补上，免得以后加字段要手工改表
        cols = {r[1] for r in conn.execute("PRAGMA table_info(kb_document_mapping)")}
        for col in ("source_url", "src_file", "src_updated_at", "category",
                    "author", "secret_level"):
            if col not in cols:
                conn.execute(f"ALTER TABLE kb_document_mapping ADD COLUMN {col} TEXT")


# 进程一启动就建表。放在 __main__ 里的话，换成 waitress 跑就没有表了
init_db()


def find_document(article_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT document_id, source_url, src_updated_at FROM kb_document_mapping "
            "WHERE dataset_id = ? AND article_id = ? AND status = 1",
            (DATASET_ID, article_id),
        ).fetchone()
    return (row[0], row[1], row[2]) if row else (None, None, None)


def save_mapping(article_id, document_id, doc_name, src_file, source_url,
                 src_updated_at, category, author, secret_level):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO kb_document_mapping
                (document_id, dataset_id, article_id, doc_name, src_file, source_url,
                 src_updated_at, category, author, secret_level, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(dataset_id, article_id) DO UPDATE SET
                document_id    = excluded.document_id,
                doc_name       = excluded.doc_name,
                src_file       = COALESCE(excluded.src_file, kb_document_mapping.src_file),
                source_url     = COALESCE(excluded.source_url, kb_document_mapping.source_url),
                src_updated_at = COALESCE(excluded.src_updated_at, kb_document_mapping.src_updated_at),
                category       = COALESCE(excluded.category, kb_document_mapping.category),
                author         = COALESCE(excluded.author, kb_document_mapping.author),
                secret_level   = COALESCE(excluded.secret_level, kb_document_mapping.secret_level),
                status         = 1,
                updated_at     = CURRENT_TIMESTAMP
            """,
            (document_id, DATASET_ID, article_id, doc_name, src_file, source_url,
             src_updated_at, category, author, secret_level),
        )


def mark_offline(article_id):
    with connect() as conn:
        conn.execute(
            "UPDATE kb_document_mapping SET status = 0, updated_at = CURRENT_TIMESTAMP "
            "WHERE dataset_id = ? AND article_id = ?",
            (DATASET_ID, article_id),
        )


def online_articles():
    with connect() as conn:
        return conn.execute(
            "SELECT article_id, document_id FROM kb_document_mapping "
            "WHERE dataset_id = ? AND status = 1",
            (DATASET_ID,),
        ).fetchall()


def get_cfg(key):
    with connect() as conn:
        row = conn.execute("SELECT v FROM kb_config WHERE k = ?", (key,)).fetchone()
    return row[0] if row else None


def set_cfg(key, value):
    with connect() as conn:
        conn.execute(
            "INSERT INTO kb_config (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, value),
        )


# ---------------------------------------------------------------- Dify
def _headers():
    return {"Authorization": f"Bearer {DIFY_API_KEY}"}


def _upload(url, filename, stream):
    stream.seek(0)
    files = {
        "file": (filename, stream,
                 mimetypes.guess_type(filename)[0] or "application/octet-stream"),
        "data": (None, json.dumps(PROCESS_RULE), "text/plain"),
    }
    return requests.post(url, headers=_headers(), files=files, timeout=DIFY_TIMEOUT)


def call_dify(filename, stream, document_id=None):
    """document_id 为空走新增，否则走覆盖更新。返回 (document_id, batch)。"""
    create_url = f"{DIFY_BASE}/datasets/{DATASET_ID}/document/create-by-file"

    if not document_id:
        resp = _upload(create_url, filename, stream)
    else:
        update_url = (f"{DIFY_BASE}/datasets/{DATASET_ID}/documents/"
                      f"{document_id}/update-by-file")
        resp = _upload(update_url, filename, stream)

        # Dify 只在文档 display_status == "available" 时才允许原地更新，
        # 也就是必须「索引完成 + 未归档 + 已启用」。文档还在排队/切分/索引，
        # 或上次索引失败、被别人停用时，都会回这个 400。
        # 这种状态等下去也没意义，直接删掉重建，保证这次推送一定能落地。
        if resp.status_code == 400 and "not available" in resp.text:
            log.warning("document_id=%s 当前不可原地更新，改为删除重建：%s",
                        document_id, resp.text[:150])
            delete_dify_document(document_id)
            resp = _upload(create_url, filename, stream)

    if resp.status_code != 200:
        raise RuntimeError(f"Dify 返回 {resp.status_code}: {resp.text[:500]}")

    try:
        body = resp.json()
        return body["document"]["id"], body.get("batch")
    except (ValueError, KeyError) as exc:
        raise RuntimeError(f"Dify 响应解析失败: {resp.text[:500]}") from exc


def delete_dify_document(document_id):
    url = f"{DIFY_BASE}/datasets/{DATASET_ID}/documents/{document_id}"
    resp = requests.delete(url, headers=_headers(), timeout=DIFY_TIMEOUT)
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"Dify 删除返回 {resp.status_code}: {resp.text[:500]}")


_meta_lock = threading.Lock()


def ensure_meta_field(name):
    """拿到元数据字段的 id；不存在就建一个。结果落地到 kb_config，之后不再重复请求。"""
    # 缓存键一定要带上 dataset_id。元数据字段的 id 是挂在具体知识库下的，
    # 换了知识库还复用老 id 的话，Dify 会回 404 Metadata not found。
    cache_key = f"meta_field_id:{DATASET_ID}:{name}"
    with _meta_lock:
        meta_id = get_cfg(cache_key)
        if meta_id:
            return meta_id
        resp = requests.get(f"{DIFY_BASE}/datasets/{DATASET_ID}/metadata",
                            headers=_headers(), timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"读取元数据字段失败 {resp.status_code}: {resp.text[:300]}")
        for item in resp.json().get("doc_metadata", []):
            if item.get("name") == name:
                set_cfg(cache_key, item["id"])
                return item["id"]

        resp = requests.post(f"{DIFY_BASE}/datasets/{DATASET_ID}/metadata",
                             headers=_headers(),
                             json={"type": "string", "name": name}, timeout=30)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"创建元数据字段失败 {resp.status_code}: {resp.text[:300]}")
        meta_id = resp.json()["id"]
        set_cfg(cache_key, meta_id)
        log.info("已创建 Dify 元数据字段 %s (id=%s)", name, meta_id)
        return meta_id


def attach_meta(document_id, values):
    """把业务字段写进 Dify 文档元数据。纯附加信息，失败只记日志、不影响推送结果。"""
    items = []
    for name in META_FIELDS:
        value = values.get(name)
        if not value:
            continue
        try:
            items.append({"id": ensure_meta_field(name), "name": name, "value": value})
        except Exception:  # noqa: BLE001
            log.exception("元数据字段 %s 准备失败", name)
    if not items:
        return
    try:
        resp = requests.post(
            f"{DIFY_BASE}/datasets/{DATASET_ID}/documents/metadata",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"operation_data": [{
                "document_id": document_id,
                "metadata_list": items,
                "partial_update": True,
            }]},
            timeout=30,
        )
        if resp.status_code != 200:
            log.warning("写元数据失败 %s: %s", resp.status_code, resp.text[:300])
        else:
            log.info("已写入元数据 %s (document_id=%s)",
                     {i["name"]: i["value"] for i in items}, document_id)
    except Exception:  # noqa: BLE001  元数据是附加能力，绝不让它拖垮主流程
        log.exception("写元数据异常 document_id=%s", document_id)


# ---------------------------------------------------------------- 业务动作
def _safe_name(title, fallback):
    """知识库里的文档名用文章标题，顺手把文件名里不合法的字符去掉。"""
    name = (title or "").strip() or (fallback or "").strip()
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip(" .")
    return (name[:120] or "未命名文章")


def publish(article_id, doc_name, filename, stream, values):
    """新增或覆盖更新一条。返回 dict 给响应体用。"""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        raise ItemError("file_name", f"不支持的文件类型 {ext or '(无扩展名)'}，"
                                     f"只收 {'/'.join(sorted(e.lstrip('.') for e in ALLOWED_EXT))}")

    existing, old_url, old_upd = find_document(article_id)
    updated_at = values.get("updated_at")

    # 带了 updated_at 且和上次一样，说明文章没动过，不重复上传、不重复消耗索引
    if existing and updated_at and old_upd and updated_at == old_upd:
        return {"article_id": article_id, "document_id": existing, "result": "skipped"}

    document_id, batch = call_dify(doc_name + ext, stream, existing)
    save_mapping(article_id, document_id, doc_name, values.get("src_file"),
                 values.get("source_url") or old_url, updated_at,
                 values.get("category"), values.get("author"),
                 values.get("secret_level"))
    # article_id 一并交给元数据：检索后鉴权拿到的就是正整数文章编号，
    # 直接和这一项比就能判断这条命中的文档员工有没有权限看。
    attach_meta(document_id, dict(values,
                                  article_id=str(article_id),
                                  source_url=values.get("source_url") or old_url))
    return {
        "article_id": article_id,
        "document_id": document_id,
        "batch": batch,
        "result": "updated" if existing else "created",
    }


def offline(article_id):
    """下线：知识库里删掉，映射表标记 status=0。"""
    existing, _, _ = find_document(article_id)
    if not existing:
        return {"article_id": article_id, "result": "nothing_to_do"}
    delete_dify_document(existing)
    mark_offline(article_id)
    log.info("已下线 article_id=%s document_id=%s", article_id, existing)
    return {"article_id": article_id, "document_id": existing, "result": "deleted"}


def download(url):
    if not re.match(r"^https?://", url or "", re.I):
        raise ItemError("file_url", "只接受 http/https 地址")
    try:
        resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)
    except requests.RequestException as exc:
        raise ItemError("file_url", f"下载失败: {exc}") from exc
    if resp.status_code != 200:
        raise ItemError("file_url", f"下载失败，对方返回 {resp.status_code}")
    buf = io.BytesIO()
    for chunk in resp.iter_content(64 * 1024):
        buf.write(chunk)
        if buf.tell() > MAX_FILE_BYTES:
            raise ItemError("file_url", "文件超过 %dMB" % (MAX_FILE_BYTES // 1024 // 1024))

    name = (resp.url.split("?")[0].rstrip("/").split("/")[-1]) or "unnamed"
    buf.seek(0)
    return buf, name


# ---------------------------------------------------------------- 路由
def _bad_token():
    return jsonify({"code": 403, "status": "error", "msg": "同步接口验证失败"}), 403


def _check_token():
    token = request.headers.get("X-KB-Sync-Token") or request.form.get("token") or ""
    return bool(SYNC_TOKEN) and token == SYNC_TOKEN


@app.route("/sync/kb/article/push", methods=["POST"])
def push_batch():
    """对接文档里的正式入口：一次一批，逐条校验，一条不合格只退那一条。"""
    if not _check_token():
        return _bad_token()

    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        return jsonify({"code": 400, "status": "error",
                        "msg": "请求体必须是 JSON，且 items 是数组"}), 400

    items = body["items"]
    if len(items) > MAX_BATCH:
        return jsonify({"code": 400, "status": "error",
                        "msg": f"单次最多 {MAX_BATCH} 条，本次 {len(items)} 条"}), 400

    accepted, errors, seen = 0, [], set()
    for idx, item in enumerate(items):
        try:
            if not isinstance(item, dict):
                raise ItemError("items", "每一项必须是对象")
            raw_id = item.get("article_id")
            if raw_id is None or isinstance(raw_id, bool) or \
                    not str(raw_id).strip().lstrip("-").isdigit():
                raise ItemError("article_id", "article_id 必须是正整数")
            article_id = int(raw_id)
            if article_id <= 0:
                raise ItemError("article_id", "article_id 必须是正整数")
            seen.add(article_id)

            status = (item.get("publish_status") or "published").strip()
            if status not in ("published", "unpublished", "deleted"):
                raise ItemError("publish_status",
                                "只能是 published / unpublished / deleted")

            # 知识库里只保留「已发布」的文章：
            # deleted = 文章删了，unpublished = 撤回了发布，两种都从库里摘掉，
            # 否则员工还能检索到已经下架的旧内容。
            if status in ("deleted", "unpublished"):
                offline(article_id)
                accepted += 1
                continue

            file_name = (item.get("file_name") or "").strip()
            file_url = (item.get("file_url") or "").strip()
            if not file_name:
                raise ItemError("file_name", "file_name 不能为空")
            ext = os.path.splitext(file_name)[1].lower()
            if ext not in ALLOWED_EXT:
                raise ItemError("file_name", f"不支持的文件类型 {ext or '(无扩展名)'}")
            if not file_url:
                raise ItemError("file_url", "file_url 不能为空")

            stream, _ = download(file_url)
            values = {
                "src_file": file_name,
                "source_url": item.get("source_url") or file_url,
                "updated_at": item.get("updated_at"),
                "category": item.get("category"),
                "author": item.get("author"),
                "secret_level": item.get("secret_level"),
            }
            doc_name = _safe_name(item.get("title"), os.path.splitext(file_name)[0])
            result = publish(article_id, doc_name, file_name, stream, values)
            log.info("第 %s 条 article_id=%s -> %s", idx, article_id, result)
            accepted += 1
        except ItemError as exc:
            errors.append({"index": idx, "article_id": item.get("article_id")
                           if isinstance(item, dict) else None,
                           "field": exc.field, "reason": exc.reason})
        except RuntimeError as exc:
            log.exception("第 %s 条推送失败", idx)
            errors.append({"index": idx, "article_id": item.get("article_id")
                           if isinstance(item, dict) else None,
                           "field": "dify", "reason": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log.exception("第 %s 条处理异常", idx)
            errors.append({"index": idx, "field": "unknown", "reason": str(exc)})

    # 全量批次：没出现在这一批里的文章下线。只在整批无错时执行，避免误删
    swept = 0
    if body.get("full_sync") is True:
        if errors:
            log.warning("full_sync 批次里有错误，跳过下线扫描")
        else:
            for article_id, _doc in online_articles():
                if article_id in seen:
                    continue
                try:
                    offline(article_id)
                    swept += 1
                except Exception:  # noqa: BLE001
                    log.exception("全量下线失败 article_id=%s", article_id)

    data = {"accepted": accepted, "rejected": len(errors), "offline": swept,
            "errors": errors}
    if errors:
        return jsonify({"code": 200, "status": "partial",
                        "msg": "部分数据未通过校验", "data": data})
    return jsonify({"code": 200, "status": "success", "msg": "", "data": data})


@app.route("/api/sync_article", methods=["POST"])
def sync_article():
    """multipart 单条推送：文件直接带过来，不麻烦对方开下载口子。"""
    if not _check_token():
        return _bad_token()

    raw_id = (request.form.get("article_id") or "").strip()
    if not raw_id:
        return jsonify({"code": 400, "status": "error", "msg": "缺少 article_id"}), 400
    if not raw_id.isdigit() or int(raw_id) <= 0:
        return jsonify({"code": 400, "status": "error", "msg": "article_id 必须是正整数"}), 400

    article_id = int(raw_id)
    status = (request.form.get("publish_status") or "published").strip()

    try:
        # 和 /sync/kb/article/push 一样的口径：不是 published 就从知识库摘掉
        if status in ("deleted", "unpublished"):
            data = offline(article_id)
            data["publish_status"] = status
            return jsonify({"code": 200, "status": "success",
                            "msg": "已从知识库下线", "data": data})

        file = request.files.get("file")
        if not file or not file.filename:
            return jsonify({"code": 400, "status": "error", "msg": "缺少 file"}), 400

        values = {
            "src_file": file.filename,
            "source_url": (request.form.get("source_url") or "").strip() or None,
            "updated_at": (request.form.get("updated_at") or "").strip() or None,
            "category": (request.form.get("category") or "").strip() or None,
            "author": (request.form.get("author") or "").strip() or None,
            "secret_level": (request.form.get("secret_level") or "").strip() or None,
        }
        doc_name = _safe_name(request.form.get("title"),
                              os.path.splitext(file.filename)[0])
        data = publish(article_id, doc_name, file.filename, file.stream, values)
    except ItemError as exc:
        return jsonify({"code": 400, "status": "error", "msg": exc.reason}), 400
    except RuntimeError as exc:
        log.exception("推送失败 article_id=%s", article_id)
        return jsonify({"code": 500, "status": "error", "msg": "同步失败",
                        "details": str(exc)}), 500

    log.info("article_id=%s 结果=%s", article_id, data)
    return jsonify({"code": 200, "status": "success", "msg": "", "data": data})


@app.route("/api/article_meta", methods=["GET"])
def article_meta():
    """给业务系统反查用：拿 article_id 换 document_id 和源文件 url。"""
    if not _check_token():
        return _bad_token()
    raw_id = (request.args.get("article_id") or "").strip()
    if not raw_id.isdigit():
        return jsonify({"code": 400, "status": "error", "msg": "article_id 必须是正整数"}), 400

    with connect() as conn:
        row = conn.execute(
            "SELECT document_id, doc_name, source_url, status FROM kb_document_mapping "
            "WHERE dataset_id = ? AND article_id = ?",
            (DATASET_ID, int(raw_id)),
        ).fetchone()
    if not row:
        return jsonify({"code": 404, "status": "error",
                        "msg": "没有这个 article_id 的记录"}), 404
    return jsonify({
        "code": 200,
        "status": "success",
        "data": {
            "article_id": int(raw_id),
            "document_id": row[0],
            "doc_name": row[1],
            "source_url": row[2],
            "online": bool(row[3]),
        },
    })


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({
        "status": "ok",
        "dataset_id": DATASET_ID,
        "dify_base": DIFY_BASE,
        "key_loaded": bool(DIFY_API_KEY),
        "online_articles": len(online_articles()),
    })


if __name__ == "__main__":
    if not DIFY_API_KEY or not SYNC_TOKEN:
        raise SystemExit("请先设置 DIFY_API_KEY 和 KB_SYNC_TOKEN 环境变量")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8090")))
