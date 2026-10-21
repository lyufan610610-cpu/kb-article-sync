# kb-article-sync

把业务系统的文章推送进 Dify 知识库的同步服务。它解决的不只是「把文件传进去」：在入库的同时，把 **`article_id`（文章编号）** 和 **`source_url`（原文地址）** 写入 Dify 文档元数据，后续检索命中后可以直接：

- 用 `article_id` 和业务系统做权限比对（员工有没有权限看这篇）；
- 用 `source_url` 在答案里回链原文。

服务与 Dify 源码目录完全隔离，只通过 HTTP 调用 Dify 的 Service API。

## 目录结构

```
.
├── post.py            接口代码
├── env.example.sh     密钥/配置示例（复制为 env.sh 后填写）
├── requirements.txt   Python 依赖
├── run.sh             前台启动（调试用）
├── tmux.sh            后台常驻：start / stop / restart / status / logs / attach
├── start.sh           等同 ./tmux.sh start
├── stop.sh            等同 ./tmux.sh stop
├── selftest.sh        自测脚本，一条命令验完所有行为
├── mapping.db         article_id <-> Dify document_id 映射表（运行时生成，不入库）
└── logs/app.log       日志（运行时生成，不入库）
```

## 为什么不会影响 Dify 源码更新

- 本服务放在 Dify 源码目录的**同级目录**，没有一行代码写进 Dify。
- 不 `import` 任何 Dify 代码，不共享它的 Python 环境和依赖，自带 `venv/`。
- 只通过 HTTP 调用 Dify 的 Service API，和浏览器访问走同一个口子。
- 因此在 Dify 目录里 `git pull / rebase / 改代码 / 重新 build`，本服务毫无感知；反过来本服务重启也不影响 Dify。
- 唯一耦合点是 Dify 的 Service API 契约。升级 Dify 后如果接口有变动，只需要改 `post.py`。

## 快速开始

```bash
cd /opt/kb-sync                # 换成你的部署目录
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

cp env.example.sh env.sh
vi env.sh                     # 填入 DIFY_API_KEY / KB_SYNC_TOKEN / DIFY_DATASET_ID
chmod 600 env.sh

./tmux.sh start
./selftest.sh
```

## 日常操作

```bash
./tmux.sh start      # 启动（并自动打一次 /healthz）
./tmux.sh status     # 看状态
./tmux.sh stop       # 停止
./tmux.sh restart    # 重启
./tmux.sh logs       # 实时看日志（Ctrl+C 只是退出，服务不停）
./tmux.sh attach     # 进会话看输出（退出按 Ctrl+B，松开再按 D，服务不停）
./selftest.sh        # 自测
```

改端口/密钥：编辑 `env.sh`，然后 `./tmux.sh restart`。

### 为什么用 tmux

服务跑在一个独立的 tmux 会话里，**ssh 断开、终端关掉都不会把它带走**，效果和 systemd 常驻一样，但全程普通用户权限，不需要 `sudo`。

注意：机器重启后 tmux 会话没了，服务不会自己起来。重启后执行一次 `./tmux.sh start` 即可（日志追加写，不丢历史）。

如果同一台机器上还有别人的 tmux 会话：

- 不要执行 `tmux kill-server`，那会顺手把别人的会话也杀掉；
- 停服务只用 `./tmux.sh stop`，它只动 `kb-sync` 这一个会话。

## 环境变量

| 变量 | 说明 |
|---|---|
| `DIFY_API_KEY` | 知识库的 Service API 密钥 |
| `KB_SYNC_TOKEN` | 给对方调用的固定令牌 |
| `DIFY_DATASET_ID` | 目标知识库 id |
| `DIFY_BASE` | Dify Service API 地址，例如 `http://127.0.0.1:8080/v1` |
| `PORT` | 本服务端口，默认 8090 |
| `META_FIELDS` | 额外写进 Dify 文档元数据的业务字段，逗号分隔，默认 `source_url,article_id`；`article_id` 是检索后鉴权比对用的，不要去掉 |

元数据字段的 id 是挂在**具体知识库**下的。换 `DIFY_DATASET_ID` 之后不用手工清缓存（缓存键里带了 dataset_id）；但如果是从更早版本升上来的，建议先把 `kb_config` 表清空一次。

## 对外接口

监听 `0.0.0.0:8090`。所有接口都要带 `X-KB-Sync-Token`，不对返回 403。

### 1. 批量推送 `POST /sync/kb/article/push`（正式入口）

`Content-Type: application/json`，单次最多 200 条。

```json
{
  "full_sync": false,
  "items": [
    {
      "article_id": 17,
      "title": "研发测试规范",
      "file_name": "研发测试规范.docx",
      "file_url": "https://cms.example.com/files/17.docx",
      "source_url": "https://cms.example.com/article/17",
      "publish_status": "published",
      "updated_at": "2026-09-20T14:48:00+08:00",
      "category": "技术中心/研发规范",
      "author": "研发中心",
      "secret_level": "内部"
    }
  ]
}
```

逐条校验，一条不合格只退那一条：

```json
{"code":200,"status":"partial","msg":"部分数据未通过校验",
 "data":{"accepted":1,"rejected":1,"offline":0,
         "errors":[{"index":1,"article_id":21,"field":"file_name",
                    "reason":"file_name 不能为空"}]}}
```

`full_sync: true` 时，不在本批的在线文章会被下线；**只有本批零错误才会执行下线扫描**，避免脏数据误删整库。

### 2. 单条推送 `POST /api/sync_article`

文件直接带过来时用，`multipart/form-data`。

| 字段 | 必填 | 说明 |
|---|---|---|
| article_id | 是 | 文章 id，正整数 |
| file | 是（published 时） | 文件本体，扩展名限 txt/markdown/md/mdx/pdf/html/htm/xlsx/xls/docx/csv/vtt/properties/odt/pptx |
| title | 否 | 文章标题，用作知识库文档名；不给就用文件名 |
| source_url | 建议 | 原文地址，只存不下载 |
| publish_status | 否 | `published`（默认）/ `unpublished` / `deleted` |

```bash
curl -F "article_id=999" -F "title=演示文档" \
     -F "source_url=https://cms.example.com/article/999" \
     -F "file=@/tmp/demo.md" \
     -H "X-KB-Sync-Token: $KB_SYNC_TOKEN" \
     http://127.0.0.1:8090/api/sync_article
```

### 3. 反查 `GET /api/article_meta?article_id=999`

拿 article_id 换 `document_id` / `doc_name` / `source_url` / `online`。

### 4. 健康检查 `GET /healthz`

返回 `online_articles`，即当前在线的文章条数，可以拿它当对账指标。

## 内部行为

- **文档名**用 `title`（去掉 `\ / : * ? " < > |` 等非法字符），不是 `file_name`。
- **`updated_at` 相同则跳过**，不重复上传、不重复消耗索引额度。所以对方最好稳定地传这个值。
- **原地更新失败会删除重建**：Dify 只在文档「索引完成 + 未归档 + 已启用」时允许 `update-by-file`，文档还在索引中、或上次索引失败时会回 400 `Document is not available`。这时本服务会自动删掉旧文档重新上传，日志里打印 `当前不可原地更新，改为删除重建`。
- **元数据写入是尽力而为**：`META_FIELDS` 里的字段会自动在 Dify 建好并写入文档元数据，失败只记日志、不会让推送整体失败。默认写两项：`source_url`（答案回链原文）和 `article_id`（检索后鉴权比对，值是正整数编号的字符串形式）。
- **`publish_status`**：只有 `published` 才入库。`unpublished`（撤回发布）和 `deleted`（文章删除）都会把这个 `article_id` 从知识库里摘掉，并把映射标记为下线，避免员工还能检索到已经下架的旧内容。

## 切分策略

`post.py` 里的 `PROCESS_RULE` 显式指定了父子分段：

- `doc_form = hierarchical_model`（这是顶层字段，Dify 靠它选分段器，缺了会静默落回普通切分）；
- 父块 `max_tokens=1024`，只作为上下文回带给模型，不进向量库；
- 子块 `max_tokens=200`，真正被向量化、被检索命中的单元。

父块/子块参数需要和你的文档形态一起调；改完重新推送（或先删文档）才会生效。

## 自测

一条命令跑完所有行为，跑完自动清理。全程只用假的 `article_id`（`999999001`），**不会动真实文章**：

```bash
./tmux.sh start
./selftest.sh
```

覆盖：无令牌被 403 挡掉、新增、`updated_at` 没变则跳过、覆盖更新、坏数据只退单条、`/api/article_meta` 反查、直接问 Dify 确认 `source_url` 和 `article_id` 两项元数据都写进去了、撤回发布确实把文档摘掉、`full_sync` 下线，以及「批次里有错误就绝不下线」的安全闸，最后把测试数据删干净并核对条数复原。

两点说明：

- `full_sync` 下线测试**只在知识库为空时才会跑**，避免误删真实数据。
- 脚本直接读 `env.sh` 里的密钥去问 Dify，所以要在服务器上跑。

## 日志与数据

- 日志：`logs/app.log`
- 映射表：`sqlite3 mapping.db "select article_id, document_id, source_url, status from kb_document_mapping;"`

## 安全提示

- `env.sh` 权限设为 600，且已在 `.gitignore` 中排除，不要提交到仓库。
- `KB_SYNC_TOKEN` 建议用足够长的随机串，泄露后立即更换并重启服务。
- `mapping.db` 含文章编号与文档 id 的对应关系，同样不要入库或外发。
