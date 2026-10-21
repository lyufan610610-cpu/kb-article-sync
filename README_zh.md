# KB-Article-Sync: 企业业务系统到 Dify 知识库同步网关

[English](README.md) | [中文](README_zh.md)

一套解耦、生产就绪的知识库同步网关服务，用于将企业业务系统（CMS、OA、内部知识库、工单系统）的文章与文档自动化推送至 **Dify 知识库（RAG）**。它在文档入库的同时，自动回填 **`article_id`（业务文章编号）** 与 **`source_url`（原文链接）** 两项核心元数据，支撑检索后鉴权（RBAC）与答案溯源，并内置父子层级切分策略与增量幂等控制。

---

## 核心架构与业务价值

在企业级 RAG 落地过程中，单纯将外部文件“扔”进知识库往往无法满足业务合规与交互需求：
1. **检索后权限校验（RBAC / ACL）**：知识库检索命中知识片段后，必须依据企业自身的文章编号（`article_id`）反查当前员工是否有权限查阅该文档，无权则过滤；
2. **答案原文回链**：大模型生成回复后，前端需要提供可点击的原文链接（`source_url`），方便员工溯源核实；
3. **架构解耦与零侵入**：若直接在 Dify 源码中侵入编写业务对接代码，将导致 Dify 上游升级、合并分支或拉取镜像变得极其繁琐；
4. **增量幂等与成本控制**：业务系统频繁推送，若缺乏去重机制，反复解析与向量化将耗费大量 Embedding Token 与计算资源。

`kb-article-sync` 作为独立微服务部署，与 Dify 仅通过标准 HTTP Service API 交互，双方生命周期完全解耦。

```
+----------------------------------------------------------------------------------------------------+
|                                      企业知识同步整体架构                                          |
|                                                                                                    |
|  +--------------------+                                                                            |
|  | 企业业务系统 (CMS) |                                                                            |
|  | OA / 内部知识库    |                                                                            |
|  +--------------------+                                                                            |
|            |                                                                                       |
|            | 1. 批量 / 单条推送 (JSON / Multipart 表单)                                            |
|            v                                                                                       |
|  +----------------------------------------------------------------------------------------------+  |
|  |                                  kb-article-sync 同步网关                                    |  |
|  |                                                                                              |  |
|  |  +------------------------+  +-------------------------+  +-------------------------------+  |  |
|  |  | 统一令牌鉴权与隔离    |  | 时间戳增量比对缓存      |  | 父子层级分段策略预设          |  |  |
|  |  | (X-KB-Sync-Token)      |  | (SQLite mapping.db)     |  | (1024 父块 / 200 子块)        |  |  |
|  |  +------------------------+  +-------------------------+  +-------------------------------+  |  |
|  |              |                            |                               |                  |  |
|  |              v                            v                               v                  |  |
|  |  +----------------------------------------------------------------------------------------+  |  |
|  |  | 元数据自动绑定 (source_url / article_id) 与 原地更新 / 删除重建状态机                 |  |  |
|  |  +----------------------------------------------------------------------------------------+  |  |
|  +----------------------------------------------------------------------------------------------+  |
|            |                                                                                       |
|            | 2. 标准 Service REST API 调用 (HTTP)                                                  |
|            v                                                                                       |
|  +----------------------------------------------------------------------------------------------+  |
|  |                                      Dify 知识库服务                                         |  |
|  |                                                                                              |  |
|  |  +------------------------------+     +---------------------------------------------------+  |  |
|  |  | 知识库自定义元数据属性接口   |     | 文档向量化与检索管线                              |  |  |
|  |  | (article_id, source_url)     |     | (Hierarchical Model: 高精度命中与大上下文呈现)    |  |  |
|  |  +------------------------------+     +---------------------------------------------------+  |  |
|  +----------------------------------------------------------------------------------------------+  |
+----------------------------------------------------------------------------------------------------+
```

---

## 关键技术特性

- **完全解耦，不影响 Dify 源码升级**：服务独立部署、独立虚拟环境，只依赖 Dify 开放的 Service REST API。无论 Dify 重新构建、拉取新版本还是调整依赖，本服务不受任何影响。
- **元数据动态回填**：自动在目标知识库中注册并写入自定义元数据属性：
  - `article_id`：正整数业务编号，用于检索后的员工权限匹配；
  - `source_url`：外部文档在线地址，供前端答案直接回链。
- **开箱即用的父子（Hierarchical）分段策略**：
  - 显式声明顶层 `doc_form="hierarchical_model"` 与 `parent_mode="paragraph"`；
  - **父块（1024 tokens）**：作为上下文带入大模型生成提示词，保障语义完整；
  - **子块（200 tokens）**：高精度向量化检索单元，保障相似度召回准确率。
- **智能增量比对与防刷机制**：通过本地 SQLite（`mapping.db`）记录文章版本与时间戳（`updated_at`），内容未发生变更的文档直接跳过上传，避免无意义的二次索引消耗。
- **自愈式更新状态机**：优先调用 Dify 原地更新接口（`update-by-file`）；若文档因处于索引中、上次索引失败或被禁用导致接口返回 400 错误，服务自动捕获并无缝降级为“删除旧文档并重新创建”，确保更新成功率。
- **细粒度错误隔离与安全下线**：
  - 批量推送接口逐条校验，单条数据校验失败（如缺少文件名）仅拒绝该条，不影响整批其它正常数据；
  - 全量同步模式（`full_sync: true`）配备**零错误安全闸**：只有当当前批次错误数为 0 时，才会触发未在批次中的历史在线文档下线，防止因网络或数据错误导致知识库被意外清空。

---

## 目录结构

```
kb-article-sync/
├── post.py            # Flask 核心同步服务与状态机实现
├── env.example.sh     # 环境变量配置模板
├── requirements.txt   # Python 基础依赖（Flask, requests）
├── tmux.sh            # 守护进程管理脚本（start / stop / restart / status / logs）
├── start.sh           # 启动快捷入口
├── stop.sh            # 停止快捷入口
├── run.sh             # 前台调试启动脚本
├── selftest.sh        # 全链路自动化集成自测脚本
├── mapping.db         # 本地映射缓存表（运行时自动生成，已 gitignore）
└── logs/app.log       # 运行日志（运行时自动生成，已 gitignore）
```

---

## 快速上手

### 1. 部署与安装依赖
在服务器环境（如 `/opt/kb-sync`）中初始化：

```bash
cd /opt/kb-sync
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 2. 配置环境变量
复制配置模板并填写 Dify 知识库密钥与对应 ID：

```bash
cp env.example.sh env.sh
chmod 600 env.sh
vi env.sh
```

### 3. 启动常驻服务并执行自测
```bash
# 启动后台服务
./tmux.sh start

# 查看服务状态
./tmux.sh status

# 运行自动化集成测试验证全链路
./selftest.sh
```

---

## 环境变量说明

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DIFY_API_KEY` | `""` | Dify 知识库 Service API 密钥（以 `app-...` 开头） |
| `KB_SYNC_TOKEN` | `""` | 业务方调用本同步服务时在 `X-KB-Sync-Token` 头中附带的鉴权令牌 |
| `DIFY_DATASET_ID` | `00000000-...` | 目标 Dify 知识库的 UUID |
| `DIFY_BASE` | `http://127.0.0.1:8080/v1` | Dify Service API 基础访问地址 |
| `PORT` | `8090` | 本同步服务的监听端口 |
| `META_FIELDS` | `source_url,article_id` | 额外同步至 Dify 的元数据字段名称列表（逗号分隔） |
| `MAPPING_DB` | `./mapping.db` | 本地 SQLite 映射数据库存储路径 |

---

## 接口规范

所有接口请求均需携带鉴权请求头：
```http
X-KB-Sync-Token: <KB_SYNC_TOKEN>
```

### 1. 批量文章推送
- **路由**：`POST /sync/kb/article/push`
- **Content-Type**：`application/json`
- **单批上限**：200 条

#### 请求示例
```json
{
  "full_sync": false,
  "items": [
    {
      "article_id": 1001,
      "title": "研发架构规范与最佳实践",
      "file_name": "engineering_standards.docx",
      "file_url": "https://cms.internal.example.com/files/1001.docx",
      "source_url": "https://cms.internal.example.com/article/1001",
      "publish_status": "published",
      "updated_at": "2026-10-20T10:00:00+08:00",
      "category": "技术规范",
      "author": "技术委员会",
      "secret_level": "内部"
    }
  ]
}
```

#### 正常响应（`200 OK`）
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

若其中个别记录不合法，接口返回部分成功状态：
```json
{
  "code": 200,
  "status": "partial",
  "msg": "部分数据未通过校验",
  "data": {
    "accepted": 1,
    "rejected": 1,
    "offline": 0,
    "errors": [
      {
        "index": 1,
        "article_id": 1002,
        "field": "file_name",
        "reason": "file_name 不能为空"
      }
    ]
  }
}
```

---

### 2. 单条直接文件推送
- **路由**：`POST /api/sync_article`
- **Content-Type**：`multipart/form-data`

```bash
curl -F "article_id=1001" \
     -F "title=研发规范" \
     -F "source_url=https://cms.internal.example.com/article/1001" \
     -F "publish_status=published" \
     -F "file=@/path/to/standards.docx" \
     -H "X-KB-Sync-Token: $KB_SYNC_TOKEN" \
     http://127.0.0.1:8090/api/sync_article
```

---

### 3. 元数据与映射反查
- **路由**：`GET /api/article_meta?article_id=1001`

用于根据业务文章编号反查其在 Dify 中的文档 ID、链接及在线状态：
```json
{
  "code": 200,
  "data": {
    "article_id": 1001,
    "document_id": "4e7235a1-7734-4b5c-b17f-4c12690184b2",
    "doc_name": "研发规范",
    "source_url": "https://cms.internal.example.com/article/1001",
    "online": true
  }
}
```

---

### 4. 服务健康度检查
- **路由**：`GET /healthz`

```json
{
  "status": "ok",
  "online_articles": 1420
}
```

---

## 运维与进程管理

项目提供 `tmux.sh` 脚本，适用于普通服务器用户在非 root / 无 systemd 权限下进行服务常驻运维：

```bash
./tmux.sh start      # 后台启动服务并校验 /healthz
./tmux.sh status     # 查看当前进程状态
./tmux.sh logs       # 实时滚动查看服务日志（Ctrl+C 仅退出查看，不影响服务运行）
./tmux.sh attach     # 进入 tmux 交互终端（Ctrl+B 松开后按 D 退出并保持后台运行）
./tmux.sh restart    # 重载配置并重启
./tmux.sh stop       # 安全停止后台服务
```

---

## 全链路自动化自测（`selftest.sh`）

在已配置好 `env.sh` 的环境下直接运行：

```bash
./selftest.sh
```

**测试覆盖场景**：
1. 未授权请求拦截（无 Token / 错误 Token 返回 403）；
2. 启动临时下载源模拟业务系统文件推送；
3. 新增文档与首次入库；
4. 增量幂等校验（相同 `updated_at` 自动跳过，不重复消耗额度）；
5. 文档版本覆盖更新验证；
6. 容错测试（异常格式数据独立报错，同批正常数据继续入库）；
7. `/api/article_meta` 映射反查；
8. 对接 Dify 验证 `source_url` 与 `article_id` 元数据字段真实写入情况；
9. 撤回发布与下线逻辑；
10. 全量同步下线逻辑及自动化清理测试数据。

---

## 安全建议

- **权限防护**：确保 `env.sh` 的权限为 `600`，避免其他普通用户读取配置中的 API Key。
- **高熵令牌**：`KB_SYNC_TOKEN` 务必使用强随机字符串（建议 32 位以上随机哈希），防止接口被未授权探测。
- **数据留存**：定期对 `mapping.db` 进行备份，确保文章编号与知识库文档映射关系持久化。
