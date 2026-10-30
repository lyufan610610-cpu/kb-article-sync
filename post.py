"""
知识库文章同步接口  (研发知识库 + QA文档库 / Dify)
==================================================
部署位置 : /mnt/ssd1/rd-knowledge-base/kb-sync
重要说明 : 本文件与 Dify 源码目录（/mnt/ssd1/rd-knowledge-base/dify）完全隔离，
           不 import 任何 Dify 代码，只通过 HTTP 调用 Dify 的 Service API。
           因此 pull / rebase / 二次开发 Dify 源码都不会影响本服务。

入库分流 :
    每条文档只抽开头的正文（前几页 / 前几千字），交给本地大模型判定形态，然后推到对应的库：
        doc_type = 1 普通文档  ->  研发知识库（切分规则随请求下发，见 PROCESS_RULE_RD）
        doc_type = 2 问答文档  ->  QA文档库（切分规则在 Dify 知识库设置里维护，本服务不下发）
    判定不了（模型超时 / 正文抽不出来）一律按 1 处理，绝不阻断入库。
    调用方也可以直接传 doc_type 跳过模型判定（排查、人工改判时用）。

启动方式 :
    cd /mnt/ssd1/rd-knowledge-base/kb-sync
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

import html
import io
import json
import logging
import mimetypes
import os
import re
import sqlite3
import threading
import zipfile

import requests
from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("kb-sync")

app = Flask(__name__)


@app.before_request
def _log_incoming():
    """每条进来的请求都留一行，用于确认对方到底发没发。"""
    forwarded = request.headers.get("X-Forwarded-For")
    log.info("收到请求 %s %s from=%s ip=%s len=%s ct=%s ua=%s ref=%s",
             request.method, request.path,
             forwarded or request.remote_addr, request.remote_addr,
             request.content_length,
             request.headers.get("Content-Type"),
             request.headers.get("User-Agent"),
             request.headers.get("Referer"))

# ---------------------------------------------------------------- 配置
# 两个目标库：普通文档进研发知识库，问答型文档进 QA文档库。
DATASET_RD = os.environ.get("DIFY_DATASET_ID", "3e5855a3-f2a5-4b96-a05d-eff18406f66a")
DATASET_QA = os.environ.get("DIFY_DATASET_QA_ID", "d5582337-85ed-43a3-abc2-aa9b1c24913b")
DOC_TYPE_RD, DOC_TYPE_QA = 1, 2
DATASET_BY_TYPE = {DOC_TYPE_RD: DATASET_RD, DOC_TYPE_QA: DATASET_QA}
LIBRARY_BY_TYPE = {DOC_TYPE_RD: "研发知识库", DOC_TYPE_QA: "QA文档库"}

# 本服务就跑在服务器上，所以直接走宿主机的 127.0.0.1:8082 就够了
DIFY_BASE = os.environ.get("DIFY_BASE", "http://127.0.0.1:8082/v1")
# 环境变量里只放裸 key，Bearer 前缀在代码里拼，避免两处各写一半
DIFY_API_KEY = os.environ.get("DIFY_API_KEY", "")
SYNC_TOKEN = os.environ.get("KB_SYNC_TOKEN", "")
DB_PATH = os.environ.get(
    "MAPPING_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "mapping.db")
)

# 文档分类用的本地大模型（OpenAI 兼容接口，和 Dify 用的是同一批 vLLM 实例）。
# 判不了就按普通文档入库，所以这里不可用不影响推送，只是所有文档都会进研发知识库。
CLASSIFIER_BASE = os.environ.get("CLASSIFIER_BASE", "http://127.0.0.1:8001/v1")
CLASSIFIER_MODEL = os.environ.get("CLASSIFIER_MODEL", "deepseek-r1-14b-awq")
CLASSIFIER_KEY = os.environ.get("CLASSIFIER_API_KEY", "")
CLASSIFIER_TIMEOUT = int(os.environ.get("CLASSIFIER_TIMEOUT", "90"))
# 送模型的正文长度：只看开头这几千字，不做全文解析。
# 问答型文档从第一组问答就能看出来，长文档也只需读开头几页，分类耗时与文档大小无关。
CLASSIFIER_CHARS = int(os.environ.get("CLASSIFIER_CHARS", "3000"))
# 分类时最多翻 PDF 的前几页：正文抽取是分类里唯一和文件大小强相关的开销
CLASSIFIER_PDF_PAGES = int(os.environ.get("CLASSIFIER_PDF_PAGES", "3"))

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
PROCESS_RULE_RD = {
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

# QA 文档库的切分不在这里维护：它的分段规则（父块按「问答对」切，separator="\n\nQ:"）
# 直接配在 Dify 知识库设置里。入库时只声明 doc_form（Dify 拿它校验和库结构是否一致），
# 不带 process_rule —— Dify 就会用库自己保存的最新分段规则。
DATA_QA = {"doc_form": "hierarchical_model"}
PROCESS_RULE = PROCESS_RULE_RD

# ------------------------------------------------------- 文本流的两层规则判定
# 第一层看扩展名：只有纯文本流（txt/md）才做内容形态判断。pdf/docx/pptx/xlsx
# 的正文由 Dify 侧解析，这里不猜，继续用 PROCESS_RULE_RD。
TEXT_FORM_EXT = {".txt", ".markdown", ".md", ".mdx"}

# 第二层看正文形态：
#   qa    —— 问答稿（大量 Q/问 开头的行）：送 QA 文档库，一个问答对 = 一父块 = 一子块
#   prose —— 叙述稿（流程/纪要/规范）：先合并硬换行，再按占位符切
#   list  —— 短行堆积（目录、参数流水、表格导出）：沿用研发库默认规则
# 占位符在正文里不会出现，FixedRecursiveCharacterTextSplitter 找不到它才会走递归
# 合并，等价于「只按 max_tokens 切，边界落在 \n\n → \n → 。 上」。
CHUNK_PLACEHOLDER = "@@CHUNK@@"

_QA_LINE_RE = re.compile(r"^\s*[Qq问]\s*[:：]", re.M)
_HEADING_LINE_RE = re.compile(
    r"^(?:第[一二三四五六七八九十0-9]+[章节条]|[0-9]+(?:\.[0-9]+)*[、.．\s])"
)
_SENT_END = set("。！？；!?;）)】」”\"'.")


def _rule_hierarchical(seg_sep, seg_tokens, sub_sep, sub_tokens):
    """父子分段规则：顶层 doc_form 决定用哪个分段器，rules 里才是具体参数。"""
    return {
        "indexing_technique": "high_quality",
        "doc_form": "hierarchical_model",
        "process_rule": {
            "mode": "hierarchical",
            "rules": {
                "pre_processing_rules": [
                    {"id": "remove_extra_spaces", "enabled": True},
                    {"id": "remove_urls_emails", "enabled": False},
                ],
                "parent_mode": "paragraph",
                "segmentation": {"separator": seg_sep, "max_tokens": seg_tokens,
                                 "chunk_overlap": 0},
                "subchunk_segmentation": {"separator": sub_sep, "max_tokens": sub_tokens,
                                          "chunk_overlap": 0},
            },
        },
    }


# QA 文本：父块是完整问答对，子块不再二次切（否则 Q 行、A 行会被拆成两个碎片）
# 分隔符用正文里不存在的标记，切分时整段丢弃标记本身，问答对的「Q:」开头才不会被吃掉
QA_MARK = "@@QA@@"
RULE_TEXT_QA = _rule_hierarchical(QA_MARK, 1024, QA_MARK, 1024)
# 叙述文本：父块 1024、子块 300，切点由递归合并决定
RULE_TEXT_PROSE = _rule_hierarchical(CHUNK_PLACEHOLDER, 1024, CHUNK_PLACEHOLDER, 300)


def _decode_full(raw):
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def _text_form(text):
    """返回 (形态, 判断依据)。只看前 2 万字，形态判断不需要全文。"""
    head = text[:20000]
    body = [ln.strip() for ln in head.splitlines() if ln.strip()]
    if not body:
        return "list", "空正文"
    qa_lines = len(_QA_LINE_RE.findall(head))
    qa_ratio = qa_lines / len(body)
    if qa_lines >= 5 and qa_ratio >= 0.15:
        return "qa", f"Q行{qa_lines}/{len(body)}={qa_ratio:.0%}"
    heads = sum(1 for ln in body if _HEADING_LINE_RE.match(ln))
    sent = sum(1 for ln in body if ln[-1] in _SENT_END)
    avg_len = sum(len(ln) for ln in body) / len(body)
    # 硬换行特征：大量行不以句末标点结尾，说明原文是「一行一句被折断」的排版
    wrap_ratio = 1 - sent / len(body)
    why = f"标题行{heads} 句末行{sent}/{len(body)} 平均行长{avg_len:.0f}"
    if sent >= 2 and (heads >= 3 or avg_len >= 40 or wrap_ratio >= 0.35):
        return "prose", why
    return "list", why


def _normalize_qa(text):
    """在每个问答对前打上分隔标记：父块从「Q:」开始，答案里的空行也不会拆开问答对。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(QA_MARK, "")
    out = []
    for ln in (line.rstrip() for line in text.split("\n")):
        if _QA_LINE_RE.match(ln) and out and out[-1].strip():
            out.append("")
            out.append(QA_MARK + ln)
            continue
        out.append(ln)
    return "\n".join(out)


def _normalize_prose(text):
    """合并硬换行：段内不以句末标点结尾的行接回上一行，标题行独立成段。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paras, buf = [], []
    for ln in (line.strip() for line in text.split("\n")):
        if not ln:
            if buf:
                paras.append("".join(buf))
                buf = []
            continue
        if _HEADING_LINE_RE.match(ln):
            if buf:
                paras.append("".join(buf))
                buf = []
            paras.append(ln)
            continue
        buf.append(ln)
        if ln[-1] in _SENT_END:
            paras.append("".join(buf))
            buf = []
    if buf:
        paras.append("".join(buf))
    return "\n\n".join(p.replace(CHUNK_PLACEHOLDER, "") for p in paras if p.strip())


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


def find_all(article_id):
    """一篇 article 在所有库里的在线记录。改判分类时要把另一份摘掉，不能两个库都留。"""
    with connect() as conn:
        return conn.execute(
            "SELECT dataset_id, document_id, source_url, src_updated_at "
            "FROM kb_document_mapping "
            "WHERE article_id = ? AND status = 1",
            (article_id,),
        ).fetchall()


def find_document(article_id, dataset_id):
    for ds, document_id, source_url, src_updated_at in find_all(article_id):
        if ds == dataset_id:
            return document_id, source_url, src_updated_at
    return None, None, None


def save_mapping(dataset_id, article_id, document_id, doc_name, src_file, source_url,
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
            (document_id, dataset_id, article_id, doc_name, src_file, source_url,
             src_updated_at, category, author, secret_level),
        )


def mark_offline(article_id, dataset_id):
    with connect() as conn:
        conn.execute(
            "UPDATE kb_document_mapping SET status = 0, updated_at = CURRENT_TIMESTAMP "
            "WHERE dataset_id = ? AND article_id = ?",
            (dataset_id, article_id),
        )


def online_articles():
    """所有库的在线记录，(dataset_id, article_id, document_id)。"""
    with connect() as conn:
        return conn.execute(
            "SELECT dataset_id, article_id, document_id FROM kb_document_mapping "
            "WHERE status = 1",
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


# ---------------------------------------------------------------- 文档分类
CLASSIFY_PROMPT = (
    "你是企业知识库的文档分类器。判断下面这份文档属于哪一类：\n"
    "1 = 普通文档：以章节、条款、说明、参数表、汇报内容等叙述性结构组织的技术文档；\n"
    "2 = 问答型（QA）文档：主体内容是一问一答，问题行以 Q: / Q1: / 问： / 问题： 之类开头，"
    "答案紧随其后，全文由这样的问答对连缀而成。\n"
    "先简要判断，最后一行只输出一个数字（1 或 2），不要输出其他内容。\n\n"
    "文档名：{name}\n文档正文（节选）：\n{text}"
)

_TAG_RE = re.compile(r"<[^>]+>")
_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think>|<think\b[^>]*>.*\Z", re.S)
# docx 的 <w:t>、pptx 的 <a:t>、xlsx 的 <t>、odt 的 <text:p>，都只取元素内文本
_OOXML_TEXT_RE = re.compile(r"<(?:w:t|a:t|t|text:p)\b[^>]*>(.*?)</", re.S)


def _decode_text(raw, limit):
    """只解码开头一段（limit 个字符最多占 limit*4 字节），不做全文解码。"""
    head = raw[:limit * 4]
    for enc in ("utf-8", "gb18030"):
        # 按字节截断可能正好切在一个多字节字符中间，退掉末尾几个字节再试
        for back in range(min(4, len(head))):
            try:
                return (head[:-back] if back else head).decode(enc)
            except UnicodeDecodeError:
                continue
    return head.decode("utf-8", "ignore")


def _ooxml_text(raw, limit):
    """docx / pptx / xlsx / odt 都是 zip + XML，直接抽文本节点，不用额外的解析库。"""
    chunks, size = [], 0
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        for name in zf.namelist():
            if not name.endswith(".xml"):
                continue
            if not (name.startswith("word/") or name.startswith("ppt/slides/")
                    or name.startswith("xl/") or name == "content.xml"):
                continue
            xml = zf.read(name).decode("utf-8", "ignore")
            for match in _OOXML_TEXT_RE.findall(xml):
                text = html.unescape(_TAG_RE.sub("", match)).strip()
                if not text:
                    continue
                chunks.append(text)
                size += len(text) + 1
            if size >= limit:
                break
    return "\n".join(chunks)


def _pdf_text(raw, limit):
    try:
        from pypdf import PdfReader
    except ImportError:
        log.warning("未安装 pypdf，PDF 正文抽不出来，这篇按普通文档处理")
        return ""
    try:
        reader = PdfReader(io.BytesIO(raw))
        parts, size = [], 0
        # 只看前几页：分类要的是「开头长什么样」，后面的页数再多也不影响判断
        for page in reader.pages[:CLASSIFIER_PDF_PAGES]:
            text = page.extract_text() or ""
            parts.append(text)
            size += len(text)
            if size >= limit:
                break
        return "\n".join(parts)
    except Exception:  # noqa: BLE001  PDF 千奇百怪，抽不出来就当普通文档
        log.exception("PDF 正文抽取失败")
        return ""


def extract_text(filename, raw, limit=CLASSIFIER_CHARS):
    """抽开头一段正文用于分类。抽不出来返回空串，调用方会兜底成普通文档。"""
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        text = _pdf_text(raw, limit)
    elif ext in (".docx", ".xlsx", ".pptx", ".odt"):
        try:
            text = _ooxml_text(raw, limit)
        except Exception:  # noqa: BLE001  文件损坏 / 后缀名不符
            log.exception("OOXML 正文抽取失败 filename=%s", filename)
            text = ""
    elif ext in (".html", ".htm"):
        text = html.unescape(_TAG_RE.sub(" ", _decode_text(raw, limit)))
    else:
        text = _decode_text(raw, limit)
    return re.sub(r"[ \t\u3000]+", " ", text)[:limit]


def _parse_model_answer(answer):
    """从模型输出里取 1 / 2。R1 类模型会把思考过程带在 content 里，先剥掉。"""
    answer = _THINK_RE.sub("", answer or "")
    hits = re.findall(r"(?<!\d)([12])(?!\d)", answer)
    return int(hits[-1]) if hits else None


def classify_document(doc_name, filename, raw):
    """1 = 普通文档，2 = QA 问答文档。判不了就当普通文档，绝不阻断入库。"""
    text = extract_text(filename, raw)
    if len(text) < 40:
        log.warning("正文抽取不足（%s 字符），按普通文档处理：%s", len(text), doc_name)
        return DOC_TYPE_RD

    headers = {"Authorization": f"Bearer {CLASSIFIER_KEY}"} if CLASSIFIER_KEY else {}
    payload = {
        "model": CLASSIFIER_MODEL,
        "messages": [{"role": "user",
                      "content": CLASSIFY_PROMPT.format(name=doc_name, text=text)}],
        "temperature": 0,
        "stream": False,
    }
    try:
        resp = requests.post(f"{CLASSIFIER_BASE}/chat/completions", json=payload,
                             headers=headers, timeout=CLASSIFIER_TIMEOUT)
        if resp.status_code != 200:
            log.warning("文档分类接口返回 %s，按普通文档处理：%s",
                        resp.status_code, resp.text[:200])
            return DOC_TYPE_RD
        content = resp.json()["choices"][0]["message"].get("content")
    except Exception:  # noqa: BLE001  分类是辅助能力，挂了也要能入库
        log.exception("文档分类调用失败，按普通文档处理：%s", doc_name)
        return DOC_TYPE_RD

    doc_type = _parse_model_answer(content)
    if doc_type is None:
        log.warning("分类结果解析不出 1/2，按普通文档处理 content=%r", (content or "")[:200])
        return DOC_TYPE_RD
    log.info("分类结果 %s -> %s（%s）", doc_name, doc_type, LIBRARY_BY_TYPE[doc_type])
    return doc_type


# ---------------------------------------------------------------- Dify
def _headers():
    return {"Authorization": f"Bearer {DIFY_API_KEY}"}


def _upload(url, filename, stream, data=None):
    """data 会原样作为 multipart 的 data 字段下发，决定 Dify 用哪套分段规则。"""
    stream.seek(0)
    files = {
        "file": (filename, stream,
                 mimetypes.guess_type(filename)[0] or "application/octet-stream"),
    }
    if data is not None:
        files["data"] = (None, json.dumps(data), "text/plain")
    return requests.post(url, headers=_headers(), files=files, timeout=DIFY_TIMEOUT)


def call_dify(dataset_id, filename, stream, document_id=None, data=None):
    """document_id 为空走新增，否则走覆盖更新。返回 (document_id, batch)。"""
    create_url = f"{DIFY_BASE}/datasets/{dataset_id}/document/create-by-file"

    if not document_id:
        resp = _upload(create_url, filename, stream, data)
    else:
        update_url = (f"{DIFY_BASE}/datasets/{dataset_id}/documents/"
                      f"{document_id}/update-by-file")
        resp = _upload(update_url, filename, stream, data)

        # Dify 只在文档 display_status == "available" 时才允许原地更新，
        # 也就是必须「索引完成 + 未归档 + 已启用」。文档还在排队/切分/索引，
        # 或上次索引失败、被别人停用时，都会回这个 400。
        # 这种状态等下去也没意义，直接删掉重建，保证这次推送一定能落地。
        if resp.status_code == 400 and "not available" in resp.text:
            log.warning("document_id=%s 当前不可原地更新，改为删除重建：%s",
                        document_id, resp.text[:150])
            delete_dify_document(dataset_id, document_id)
            resp = _upload(create_url, filename, stream, data)
        # 文档在 Dify 侧被手工删掉（界面上删的）时，mapping.db 里还留着旧 id，
        # 更新会回 404。这种情况不需要删，直接按新建处理，别让整条推送失败。
        elif resp.status_code == 404:
            log.warning("document_id=%s 在 Dify 侧已不存在，改为新建：%s",
                        document_id, resp.text[:150])
            resp = _upload(create_url, filename, stream, data)

    if resp.status_code != 200:
        raise RuntimeError(f"Dify 返回 {resp.status_code}: {resp.text[:500]}")

    try:
        body = resp.json()
        return body["document"]["id"], body.get("batch")
    except (ValueError, KeyError) as exc:
        raise RuntimeError(f"Dify 响应解析失败: {resp.text[:500]}") from exc


def delete_dify_document(dataset_id, document_id):
    url = f"{DIFY_BASE}/datasets/{dataset_id}/documents/{document_id}"
    resp = requests.delete(url, headers=_headers(), timeout=DIFY_TIMEOUT)
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"Dify 删除返回 {resp.status_code}: {resp.text[:500]}")


_meta_lock = threading.Lock()


def ensure_meta_field(dataset_id, name):
    """拿到元数据字段的 id；不存在就建一个。结果落地到 kb_config，之后不再重复请求。"""
    # 缓存键一定要带上 dataset_id。元数据字段的 id 是挂在具体知识库下的，
    # 换了知识库还复用老 id 的话，Dify 会回 404 Metadata not found。
    cache_key = f"meta_field_id:{dataset_id}:{name}"
    with _meta_lock:
        meta_id = get_cfg(cache_key)
        if meta_id:
            return meta_id
        resp = requests.get(f"{DIFY_BASE}/datasets/{dataset_id}/metadata",
                            headers=_headers(), timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"读取元数据字段失败 {resp.status_code}: {resp.text[:300]}")
        for item in resp.json().get("doc_metadata", []):
            if item.get("name") == name:
                set_cfg(cache_key, item["id"])
                return item["id"]

        resp = requests.post(f"{DIFY_BASE}/datasets/{dataset_id}/metadata",
                             headers=_headers(),
                             json={"type": "string", "name": name}, timeout=30)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"创建元数据字段失败 {resp.status_code}: {resp.text[:300]}")
        meta_id = resp.json()["id"]
        set_cfg(cache_key, meta_id)
        log.info("已创建 Dify 元数据字段 %s (dataset=%s id=%s)", name, dataset_id, meta_id)
        return meta_id


def attach_meta(dataset_id, document_id, values):
    """把业务字段写进 Dify 文档元数据。纯附加信息，失败只记日志、不影响推送结果。"""
    items = []
    for name in META_FIELDS:
        value = values.get(name)
        if not value:
            continue
        try:
            items.append({"id": ensure_meta_field(dataset_id, name), "name": name, "value": value})
        except Exception:  # noqa: BLE001
            log.exception("元数据字段 %s 准备失败", name)
    if not items:
        return
    try:
        resp = requests.post(
            f"{DIFY_BASE}/datasets/{dataset_id}/documents/metadata",
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


def publish(article_id, doc_name, filename, stream, values, doc_type=None):
    """分类 -> 选库 -> 新增或覆盖更新。返回 dict 给响应体用。"""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        raise ItemError("file_name", f"不支持的文件类型 {ext or '(无扩展名)'}，"
                                     f"只收 {'/'.join(sorted(e.lstrip('.') for e in ALLOWED_EXT))}")

    # 分类和上传都要读文件，先整份读进内存（最大 15MB），后面按需 seek 回去
    stream.seek(0)
    raw = stream.read()
    stream.seek(0)

    # 文本流先做形态判断：问答稿直接定去向（不再依赖分类模型），叙述稿先规范化正文。
    # 判定只影响 txt/md；其它格式的 form 为 None，行为与改造前完全一致。
    form, form_reason = None, ""
    if ext in TEXT_FORM_EXT and raw:
        text = _decode_full(raw)
        form, form_reason = _text_form(text)
        if form == "qa":
            text = _normalize_qa(text)
            stream = io.BytesIO(text.encode("utf-8"))
            if doc_type is None:
                doc_type = DOC_TYPE_QA
        elif form == "prose":
            text = _normalize_prose(text)
            stream = io.BytesIO(text.encode("utf-8"))
        log.info("article_id=%s 文本形态=%s（%s）", article_id, form, form_reason)

    if doc_type is None:
        doc_type = classify_document(doc_name, filename, raw)
    dataset_id = DATASET_BY_TYPE[doc_type]
    # 研发知识库照旧随请求下发分段规则；QA 文档库只报 doc_form，切分以知识库设置里的为准
    if doc_type == DOC_TYPE_QA:
        upload_data = RULE_TEXT_QA if form == "qa" else DATA_QA
    elif form == "prose":
        upload_data = RULE_TEXT_PROSE
    else:
        upload_data = PROCESS_RULE_RD
    rule_name = ("text_qa" if upload_data is RULE_TEXT_QA else
                 "text_prose" if upload_data is RULE_TEXT_PROSE else
                 "qa_library" if upload_data is DATA_QA else "rd_default")
    log.info("article_id=%s 入库目标 doc_type=%s library=%s dataset=%s 分段规则=%s",
             article_id, doc_type, LIBRARY_BY_TYPE[doc_type], dataset_id, rule_name)

    # 同一篇文章以前在另一个库里（比如改版后从规范变成了问答手册），先把旧库那份删掉，
    # 否则两个库会同时命中同一篇内容，检索结果重复。
    moved_from = []
    for old_ds, old_doc, _url, _upd in find_all(article_id):
        if old_ds == dataset_id:
            continue
        try:
            delete_dify_document(old_ds, old_doc)
        except Exception:  # noqa: BLE001  删不掉不影响这次入库，下次推送会再试
            log.exception("清理旧库文档失败 dataset_id=%s document_id=%s", old_ds, old_doc)
        mark_offline(article_id, old_ds)
        moved_from.append(old_ds)
        log.info("article_id=%s 已从旧库摘除 dataset=%s document_id=%s",
                 article_id, old_ds, old_doc)

    existing, old_url, old_upd = find_document(article_id, dataset_id)
    updated_at = values.get("updated_at")

    # 带了 updated_at 且和上次一样，说明文章没动过，不重复上传、不重复消耗索引
    if existing and updated_at and old_upd and updated_at == old_upd:
        return {"article_id": article_id, "document_id": existing, "doc_type": doc_type,
                "library": LIBRARY_BY_TYPE[doc_type], "moved_from": moved_from,
                "result": "skipped"}

    document_id, batch = call_dify(dataset_id, doc_name + ext, stream, existing, upload_data)
    save_mapping(dataset_id, article_id, document_id, doc_name, values.get("src_file"),
                 values.get("source_url") or old_url, updated_at,
                 values.get("category"), values.get("author"),
                 values.get("secret_level"))
    # article_id 一并交给元数据：检索后鉴权拿到的就是正整数文章编号，
    # 直接和这一项比就能判断这条命中的文档员工有没有权限看。
    attach_meta(dataset_id, document_id,
                dict(values, article_id=str(article_id),
                     source_url=values.get("source_url") or old_url))
    return {
        "article_id": article_id,
        "document_id": document_id,
        "doc_type": doc_type,
        "library": LIBRARY_BY_TYPE[doc_type],
        "moved_from": moved_from,
        "batch": batch,
        "result": "updated" if existing and document_id == existing else "created",
    }


def offline(article_id):
    """下线：两个库里都删掉，映射表标记 status=0。"""
    rows = find_all(article_id)
    if not rows:
        return {"article_id": article_id, "result": "nothing_to_do"}
    deleted = []
    for dataset_id, document_id, _url, _upd in rows:
        delete_dify_document(dataset_id, document_id)
        mark_offline(article_id, dataset_id)
        deleted.append({"dataset_id": dataset_id, "document_id": document_id})
        log.info("已下线 article_id=%s document_id=%s dataset=%s",
                 article_id, document_id, dataset_id)
    return {"article_id": article_id,
            "document_id": deleted[0]["document_id"],
            "deleted": deleted,
            "result": "deleted"}


def download(url):
    if not re.match(r"^https?://", url or "", re.I):
        raise ItemError("file_url", "只接受 http/https 地址")
    try:
        resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)
        if resp.status_code == 400 and url.lower().startswith("http://"):
            snippet = next(resp.iter_content(200), b"") or b""
            if b"plain HTTP request was sent to HTTPS port" in snippet:
                https_url = "https://" + url[len("http://"):]
                log.warning("下载失败 url=%s 是明文 HTTP 打到 HTTPS 端口，改用 %s 重试",
                            url, https_url)
                resp = requests.get(https_url, timeout=DOWNLOAD_TIMEOUT, stream=True)
    except requests.RequestException as exc:
        log.warning("下载异常 url=%s err=%s", url, exc)
        raise ItemError("file_url", f"下载失败: {exc}") from exc
    if resp.status_code != 200:
        try:
            snippet = next(resp.iter_content(200), b"") or b""
        except Exception:
            snippet = b""
        log.warning("下载失败 url=%s final_url=%s code=%s ct=%s body=%s",
                    url, resp.url, resp.status_code,
                    resp.headers.get("Content-Type"),
                    snippet.decode("utf-8", "replace")[:200])
        body_text = re.sub(r"<[^>]+>", " ", snippet.decode("utf-8", "replace"))
        body_text = re.sub(r"\s+", " ", body_text).strip()[:120]
        raise ItemError("file_url", "下载失败，对方返回 %d：%s" % (resp.status_code, body_text))
    buf = io.BytesIO()
    for chunk in resp.iter_content(64 * 1024):
        buf.write(chunk)
        if buf.tell() > MAX_FILE_BYTES:
            raise ItemError("file_url", "文件超过 %dMB" % (MAX_FILE_BYTES // 1024 // 1024))

    name = (resp.url.split("?")[0].rstrip("/").split("/")[-1]) or "unnamed"
    log.info("下载完成 url=%s final_url=%s code=%s bytes=%s ct=%s cd=%s name=%s",
             url, resp.url, resp.status_code, buf.tell(),
             resp.headers.get("Content-Type"), resp.headers.get("Content-Disposition"),
             name)
    buf.seek(0)
    return buf, name


# ---------------------------------------------------------------- 路由
def _bad_token():
    token = request.headers.get("X-KB-Sync-Token") or request.form.get("token") or ""
    log.warning("鉴权失败 %s %s from=%s token来源=%s token长度=%s",
                request.method, request.path,
                request.headers.get("X-Forwarded-For") or request.remote_addr,
                "header" if request.headers.get("X-KB-Sync-Token") else
                ("form" if request.form.get("token") else "无"), len(token))
    return jsonify({"code": 403, "status": "error", "msg": "同步接口验证失败"}), 403


def _check_token():
    token = request.headers.get("X-KB-Sync-Token") or request.form.get("token") or ""
    if not SYNC_TOKEN:
        log.error("服务端未配置 KB_SYNC_TOKEN，所有请求都会被拒")
        return False
    return token == SYNC_TOKEN


def _parse_doc_type(raw):
    """入参可选：1 普通文档 / 2 QA 文档。不传就走模型判定。"""
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None
    if text not in ("1", "2"):
        raise ItemError("doc_type", "doc_type 只能是 1（普通文档）或 2（QA 文档）")
    return int(text)


@app.route("/sync/kb/article/push", methods=["POST"])
def push_batch():
    """对接文档里的正式入口：一次一批，逐条校验，一条不合格只退那一条。"""
    if not _check_token():
        return _bad_token()

    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        log.warning("批量推送请求体不合法 body_type=%s raw=%s",
                    type(body).__name__, request.get_data(as_text=True)[:500])
        return jsonify({"code": 400, "status": "error",
                        "msg": "请求体必须是 JSON，且 items 是数组"}), 400

    items = body["items"]
    if len(items) > MAX_BATCH:
        log.warning("批量推送超过上限 条数=%s 上限=%s", len(items), MAX_BATCH)
        return jsonify({"code": 400, "status": "error",
                        "msg": f"单次最多 {MAX_BATCH} 条，本次 {len(items)} 条"}), 400

    log.info("批量推送开始 full_sync=%s 条数=%s", body.get("full_sync") is True, len(items))
    accepted, errors, seen = 0, [], set()
    for idx, item in enumerate(items):
        if isinstance(item, dict):
            log.info("第 %s 条入参 article_id=%s publish_status=%s file_name=%s title=%s file_url=%s",
                     idx, item.get("article_id"), item.get("publish_status"),
                     item.get("file_name"), item.get("title"), item.get("file_url"))
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

            doc_type = _parse_doc_type(item.get("doc_type"))
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
            log.info("第 %s 条开始入库 article_id=%s doc_name=%s values=%s",
                     idx, article_id, doc_name, values)
            result = publish(article_id, doc_name, file_name, stream, values, doc_type)
            log.info("第 %s 条 article_id=%s -> %s", idx, article_id, result)
            accepted += 1
        except ItemError as exc:
            failed_id = item.get("article_id") if isinstance(item, dict) else None
            log.warning("article_id=%s 推送失败 file_name=%s file_url=%s field=%s reason=%s",
                        failed_id, item.get("file_name") if isinstance(item, dict) else None,
                        item.get("file_url") if isinstance(item, dict) else None,
                        exc.field, exc.reason)
            errors.append({"index": idx, "article_id": failed_id,
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
    swept, swept_ids = 0, set()
    if body.get("full_sync") is True:
        if errors:
            log.warning("full_sync 批次里有错误，跳过下线扫描")
        else:
            for _ds, article_id, _doc in online_articles():
                if article_id in seen or article_id in swept_ids:
                    continue
                try:
                    offline(article_id)
                    swept_ids.add(article_id)
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
        log.warning("单条推送缺少 article_id files=%s ct=%s len=%s",
                    list(request.files.keys()), request.content_type, request.content_length)
        return jsonify({"code": 400, "status": "error", "msg": "缺少 article_id"}), 400
    if not raw_id.isdigit() or int(raw_id) <= 0:
        log.warning("单条推送 article_id 非法 raw_id=%s", raw_id)
        return jsonify({"code": 400, "status": "error", "msg": "article_id 必须是正整数"}), 400

    article_id = int(raw_id)
    status = (request.form.get("publish_status") or "published").strip()
    _file = request.files.get("file")
    log.info("单条推送入参 article_id=%s publish_status=%s filename=%s file_ct=%s "
             "ct=%s len=%s fields=%s",
             article_id, status, _file.filename if _file else None,
             getattr(_file, "content_type", None) if _file else None,
             request.content_type, request.content_length,
             sorted(request.form.keys()))

    try:
        # 和 /sync/kb/article/push 一样的口径：不是 published 就从知识库摘掉
        if status in ("deleted", "unpublished"):
            data = offline(article_id)
            data["publish_status"] = status
            return jsonify({"code": 200, "status": "success",
                            "msg": "已从知识库下线", "data": data})

        file = request.files.get("file")
        if not file or not file.filename:
            log.warning("单条推送缺少 file article_id=%s files=%s fields=%s",
                        article_id, list(request.files.keys()), sorted(request.form.keys()))
            return jsonify({"code": 400, "status": "error", "msg": "缺少 file"}), 400

        doc_type = _parse_doc_type(request.form.get("doc_type"))
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
        data = publish(article_id, doc_name, file.filename, file.stream, values, doc_type)
    except ItemError as exc:
        log.warning("article_id=%s 推送失败 field=%s reason=%s",
                    article_id, exc.field, exc.reason)
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
            "SELECT document_id, doc_name, source_url, status, dataset_id "
            "FROM kb_document_mapping WHERE article_id = ? "
            "ORDER BY status DESC, updated_at DESC LIMIT 1",
            (int(raw_id),),
        ).fetchone()
    if not row:
        return jsonify({"code": 404, "status": "error",
                        "msg": "没有这个 article_id 的记录"}), 404
    doc_type = DOC_TYPE_QA if row[4] == DATASET_QA else DOC_TYPE_RD
    return jsonify({
        "code": 200,
        "status": "success",
        "data": {
            "article_id": int(raw_id),
            "document_id": row[0],
            "doc_name": row[1],
            "source_url": row[2],
            "online": bool(row[3]),
            "dataset_id": row[4],
            "doc_type": doc_type,
            "library": LIBRARY_BY_TYPE[doc_type],
        },
    })


@app.route("/healthz", methods=["GET"])
def healthz():
    online = online_articles()
    by_library = {}
    for dataset_id, _article_id, _document_id in online:
        key = "QA文档库" if dataset_id == DATASET_QA else (
            "研发知识库" if dataset_id == DATASET_RD else dataset_id)
        by_library[key] = by_library.get(key, 0) + 1
    return jsonify({
        "status": "ok",
        "dataset_id": DATASET_RD,
        "dataset_qa_id": DATASET_QA,
        "dify_base": DIFY_BASE,
        "key_loaded": bool(DIFY_API_KEY),
        "classifier": {"base": CLASSIFIER_BASE, "model": CLASSIFIER_MODEL},
        "online_articles": len(online),
        "online_by_library": by_library,
    })


if __name__ == "__main__":
    if not DIFY_API_KEY or not SYNC_TOKEN:
        raise SystemExit("请先设置 DIFY_API_KEY 和 KB_SYNC_TOKEN 环境变量")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8090")))
