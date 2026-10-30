# 知识库同步服务 - 环境变量示例
# 复制为 env.sh 后填入真实值，并执行 chmod 600 env.sh（只有本账号可读）。
# 用法：脚本里 set -a; . ./env.sh; set +a   （set -a 会自动 export，所以这里不写 export）

# ----------------- Dify 基础配置 -----------------
DIFY_API_KEY=dataset-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
KB_SYNC_TOKEN=change-me-to-a-long-random-string
DIFY_BASE=http://127.0.0.1:8082/v1
PORT=8090

# 两个目标知识库：普通文档进研发知识库，问答型文档进 QA 文档库
DIFY_DATASET_ID=3e5855a3-f2a5-4b96-a05d-eff18406f66a
DIFY_DATASET_QA_ID=d5582337-85ed-43a3-abc2-aa9b1c24913b

# ----------------- 本地大模型分类器配置 -----------------
# 用于新上传文档的自动形态判定（问答 vs 研发规范），判不了会自动回落到普通文档
CLASSIFIER_BASE=http://127.0.0.1:8001/v1
CLASSIFIER_MODEL=deepseek-r1-14b-awq
CLASSIFIER_API_KEY=
CLASSIFIER_TIMEOUT=90
CLASSIFIER_CHARS=3000
CLASSIFIER_PDF_PAGES=3

# ----------------- 元数据字段配置 -----------------
# 会额外写进 Dify 文档元数据的业务字段，多个用逗号分隔
# article_id 是检索后鉴权比对用的那一项，不要去掉
META_FIELDS=source_url,article_id
MAPPING_DB=./mapping.db
