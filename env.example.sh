# 知识库同步服务 - 环境变量示例
# 复制为 env.sh 后填入真实值，并执行 chmod 600 env.sh（只有本账号可读）。
# 用法：脚本里 set -a; . ./env.sh; set +a   （set -a 会自动 export，所以这里不写 export）
DIFY_API_KEY=dataset-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
KB_SYNC_TOKEN=change-me-to-a-long-random-string
DIFY_DATASET_ID=00000000-0000-0000-0000-000000000000
DIFY_BASE=http://127.0.0.1:8080/v1
PORT=8090
# 会额外写进 Dify 文档元数据的业务字段，多个用逗号分隔
# article_id 是检索后鉴权比对用的那一项，不要去掉
META_FIELDS=source_url,article_id
