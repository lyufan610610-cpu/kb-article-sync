#!/usr/bin/env bash
# ============================================================
# kb-sync 自测脚本 —— 你自己跑就行
#
# 用法：
#     cd /opt/kb-sync
#     ./tmux.sh start      # 先确保服务在跑
#     ./selftest.sh        # 再跑这个
#
# 全程只用假的 article_id（999999001），跑完会自己清理干净，
# 不会动到真实文章。每一步打印 [通过] / [失败]，最后给汇总。
#
# 它测什么：
#   1  不带令牌调接口，应该被 403 挡掉
#   2  起一个临时下载源，模拟对方给的 file_url
#   3  首次推送（新增）
#   4  同样的 updated_at 再推一次（应该跳过，不重复上传）
#   5  改 updated_at 再推（走覆盖更新）
#   6  故意混几条坏数据（好的那条应该照常入库）
#   7  反查接口 /api/article_meta
#   8  直接问 Dify：文档在不在、source_url 元数据写进去没有
#   9  全量同步 full_sync：不在本批的文章会被下线
#  10  收尾，把测试数据删干净，恢复成跑之前的样子
# ============================================================
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
WORK=/tmp/kbselftest
FILE_PORT=18099
AID=999999001
PY="$DIR/venv/bin/python"

if [ -f "$DIR/env.sh" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$DIR/env.sh"
    set +a
fi
PORT="${PORT:-8090}"
API="http://127.0.0.1:${PORT}"
FILES="http://127.0.0.1:${FILE_PORT}"
TOKEN="${KB_SYNC_TOKEN:-}"

PASS=0
FAIL=0
ok()   { echo "  [通过] $1"; PASS=$((PASS + 1)); }
bad()  { echo "  [失败] $1"; FAIL=$((FAIL + 1)); }
step() { printf '\n==== %s ====\n' "$1"; }

jget() {  # jget <json 文件> <点分路径>
    "$PY" -c '
import json, sys
try:
    cur = json.load(open(sys.argv[1]))
    for key in sys.argv[2].split("."):
        cur = cur[int(key)] if isinstance(cur, list) else cur[key]
    print(cur)
except Exception:
    pass
' "$1" "$2"
}

expect_eq() {  # expect_eq <说明> <实际> <期望>
    if [ "$2" = "$3" ]; then ok "$1（= $3）"; else bad "$1：期望 $3，实际「$2」"; fi
}

expect_has() {  # expect_has <说明> <文件> <正则>
    if grep -Eq "$3" "$2" 2>/dev/null; then ok "$1"; else bad "$1：响应里没找到 /$3/，实际：$(head -c 300 "$2")"; fi
}

expect_gone() {  # expect_gone <说明> <文件> <正则>
    if grep -Eq "$3" "$2" 2>/dev/null; then bad "$1：不该出现 /$3/，实际：$(head -c 300 "$2")"; else ok "$1"; fi
}

push() {  # push <请求体文件> <响应文件>，返回 HTTP 码
    curl -s -o "$2" -w '%{http_code}' -X POST "$API/sync/kb/article/push" \
        -H "X-KB-Sync-Token: $TOKEN" -H 'Content-Type: application/json' \
        --data-binary "@$1"
}

make_body() {  # make_body <输出文件> <true|false> <items 文本，里面可用 @AID@ / @FILES@ 占位>
    "$PY" -c '
import json, sys
out, full, items = sys.argv[1], sys.argv[2].lower() == "true", sys.argv[3]
items = items.replace("@AID@", sys.argv[5]).replace("@FILES@", sys.argv[4])
json.dump({"full_sync": full, "items": json.loads(items)}, open(out, "w"))
' "$1" "$2" "$3" "$FILES" "$AID"
}

online_count() {
    curl -s "$API/healthz" | sed -n 's/.*"online_articles": *\([0-9]\+\).*/\1/p'
}

dify_doc_count() {
    curl -s -H "Authorization: Bearer $DIFY_API_KEY" \
        "$DIFY_BASE/datasets/$DIFY_DATASET_ID/documents?limit=100" |
        "$PY" -c 'import json,sys; print(len(json.load(sys.stdin).get("data", [])))' 2>/dev/null
}

# Dify 的 JSON 会把中文转义成 \uXXXX、把 / 转义成 \/，
# 所以不能拿中文或 URL 原文去 grep，必须解析出来再比。
dify_doc_name() {  # dify_doc_name <文档详情 json>
    "$PY" -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    name = d.get("name") or (d.get("data_source_info") or {}).get("upload_file", {}).get("name") or ""
    print(name)
except Exception:
    pass
' "$1"
}

dify_meta_value() {  # dify_meta_value <文档详情 json> <元数据名>
    "$PY" -c '
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
    for m in (doc.get("doc_metadata") or []):
        if m.get("name") == sys.argv[2]:
            print(m.get("value") or "")
            break
except Exception:
    pass
' "$1" "$2"
}

HTTP_PID=""
cleanup() {
    if [ -n "$HTTP_PID" ]; then kill "$HTTP_PID" 2>/dev/null; fi
    rm -rf "$WORK"
}
trap cleanup EXIT

# ---------------------------------------------------------- 0
step "0. 前置检查"
mkdir -p "$WORK/www"
if ! curl -fsS "$API/healthz" -o "$WORK/health.json" 2>/dev/null; then
    echo "  服务没在跑。先执行：./tmux.sh start"
    echo "  （如果起了还这样，看日志：tail -n 50 $DIR/logs/app.log）"
    exit 1
fi
ok "服务在跑：$(cat "$WORK/health.json")"
if [ -z "$TOKEN" ]; then
    echo "  读不到 KB_SYNC_TOKEN，检查 $DIR/env.sh"
    exit 1
fi

BEFORE_ONLINE="$(online_count)"; BEFORE_ONLINE="${BEFORE_ONLINE:-0}"
BEFORE_DOCS="$(dify_doc_count)"; BEFORE_DOCS="${BEFORE_DOCS:-0}"
echo "  跑之前：在线文章 ${BEFORE_ONLINE} 条，Dify 里文档 ${BEFORE_DOCS} 个"

# ---------------------------------------------------------- 1
step "1. 不带令牌调用，应该被挡（期望 HTTP 403）"
CODE=$(curl -s -o "$WORK/notoken.json" -w '%{http_code}' -X POST \
    "$API/sync/kb/article/push" -H 'Content-Type: application/json' --data '{"items":[]}')
expect_eq "没有令牌的请求被拒绝" "$CODE" "403"

# ---------------------------------------------------------- 2
step "2. 起一个临时下载源（模拟对方给的 file_url）"
printf '# 自测文档\n\n这是 kb-sync 自测用的临时文件，随便删。\n' > "$WORK/www/selftest.md"
"$PY" -m http.server "$FILE_PORT" --bind 127.0.0.1 --directory "$WORK/www" \
    >"$WORK/httpd.log" 2>&1 &
HTTP_PID=$!
sleep 1
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$FILES/selftest.md")
expect_eq "临时下载源可以下到文件" "$CODE" "200"

# ---------------------------------------------------------- 3
step "3. 第一次推送（新增）"
make_body "$WORK/p1.json" false '[
 {"article_id":@AID@,"title":"kb-sync 自测文档","file_name":"selftest.md",
  "file_url":"@FILES@/selftest.md","source_url":"https://example.com/rd/a/@AID@",
  "publish_status":"published","updated_at":"2026-09-20T10:00:00Z",
  "category":"自测","author":"自测","secret_level":"内部"}]'
CODE=$(push "$WORK/p1.json" "$WORK/r1.json")
expect_eq "HTTP 状态" "$CODE" "200"
expect_eq "status" "$(jget "$WORK/r1.json" status)" "success"
expect_eq "data.accepted" "$(jget "$WORK/r1.json" data.accepted)" "1"
expect_eq "online_articles" "$(online_count)" "1"
sleep 1
if tail -n 40 "$DIR/logs/app.log" | grep -q "'result': 'created'"; then
    ok "服务日志里能看到 created"
else
    bad "服务日志里没看到 created"
fi

# ---------------------------------------------------------- 4
step "4. 用一样的 updated_at 再推一次（期望：跳过，不重复上传）"
# 注意：grep -c 没匹配到时也会打印 0，只是退出码非 0，
# 所以这里不能用 "|| echo 0"，否则变量会变成两行 "0\n0"。
BEFORE_SKIP=$(grep -c "'result': 'skipped'" "$DIR/logs/app.log" 2>/dev/null)
BEFORE_SKIP="${BEFORE_SKIP:-0}"
CODE=$(push "$WORK/p1.json" "$WORK/r2.json")
expect_eq "HTTP 状态" "$CODE" "200"
expect_eq "data.accepted" "$(jget "$WORK/r2.json" data.accepted)" "1"
sleep 1
AFTER_SKIP=$(grep -c "'result': 'skipped'" "$DIR/logs/app.log" 2>/dev/null)
AFTER_SKIP="${AFTER_SKIP:-0}"
if [ "$AFTER_SKIP" -gt "$BEFORE_SKIP" ]; then
    ok "服务日志确认跳过了（skipped +$((AFTER_SKIP - BEFORE_SKIP))）"
else
    bad "没看到 skipped，可能重复上传了一遍，白耗索引额度"
fi

# ---------------------------------------------------------- 5
step "5. 改 updated_at 再推（期望：走覆盖更新）"
make_body "$WORK/p2.json" false '[
 {"article_id":@AID@,"title":"kb-sync 自测文档","file_name":"selftest.md",
  "file_url":"@FILES@/selftest.md","source_url":"https://example.com/rd/a/@AID@",
  "updated_at":"2026-09-20T11:00:00Z"}]'
CODE=$(push "$WORK/p2.json" "$WORK/r3.json")
expect_eq "HTTP 状态" "$CODE" "200"
expect_eq "data.accepted" "$(jget "$WORK/r3.json" data.accepted)" "1"
sleep 1
if tail -n 30 "$DIR/logs/app.log" | grep -Eq "'result': 'updated'|改为删除重建"; then
    ok "走的是更新路径（日志里是 updated，或自动删除重建）"
else
    bad "更新路径没留下日志"
fi

# ---------------------------------------------------------- 6
step "6. 混几条坏数据（期望：只退坏的，好的那条照常入库）"
make_body "$WORK/p3.json" false '[
 {"article_id":"abc","title":"坏的1","file_name":"a.md","file_url":"@FILES@/selftest.md"},
 {"article_id":999999002,"title":"坏的2","file_name":"","file_url":"@FILES@/selftest.md"},
 {"article_id":999999003,"title":"坏的3","file_name":"virus.exe","file_url":"@FILES@/selftest.md"},
 {"article_id":@AID@,"title":"kb-sync 自测文档","file_name":"selftest.md",
  "file_url":"@FILES@/selftest.md","source_url":"https://example.com/rd/a/@AID@",
  "updated_at":"2026-09-20T12:00:00Z"}]'
CODE=$(push "$WORK/p3.json" "$WORK/r4.json")
expect_eq "HTTP 状态" "$CODE" "200"
expect_eq "status" "$(jget "$WORK/r4.json" status)" "partial"
expect_eq "data.accepted" "$(jget "$WORK/r4.json" data.accepted)" "1"
expect_eq "data.rejected" "$(jget "$WORK/r4.json" data.rejected)" "3"
expect_has "错误里指出了 article_id 有问题" "$WORK/r4.json" '"field": *"article_id"'
expect_has "错误里指出了 file_name 有问题" "$WORK/r4.json" '"field": *"file_name"'
expect_has "错误里带了下标 index" "$WORK/r4.json" '"index": *0'
expect_eq "库里的在线文章数没被坏数据影响" "$(online_count)" "1"

# ---------------------------------------------------------- 7
step "7. 反查 /api/article_meta"
curl -s -H "X-KB-Sync-Token: $TOKEN" \
    "$API/api/article_meta?article_id=$AID" -o "$WORK/meta.json"
expect_eq "data.article_id" "$(jget "$WORK/meta.json" data.article_id)" "$AID"
expect_eq "data.online" "$(jget "$WORK/meta.json" data.online)" "True"
expect_eq "data.source_url" "$(jget "$WORK/meta.json" data.source_url)" "https://example.com/rd/a/$AID"
expect_eq "文档名用的是 title" "$(jget "$WORK/meta.json" data.doc_name)" "kb-sync 自测文档"
DOC_ID="$(jget "$WORK/meta.json" data.document_id)"
echo "  document_id = $DOC_ID"
if [ -z "$DOC_ID" ]; then
    echo "  反查不到 document_id，后面几步跳过"
else
    # ------------------------------------------------------ 8
    step "8. 直接问 Dify：文档在不在、source_url 写进去没有"
    curl -s -H "Authorization: Bearer $DIFY_API_KEY" \
        "$DIFY_BASE/datasets/$DIFY_DATASET_ID/documents/$DOC_ID" -o "$WORK/doc.json"
    expect_has "Dify 里能找到这个文档" "$WORK/doc.json" "$DOC_ID"
    DOC_NAME="$(dify_doc_name "$WORK/doc.json")"
    if printf '%s' "$DOC_NAME" | grep -q 'kb-sync 自测文档'; then
        ok "文档名对得上（= $DOC_NAME）"
    else
        bad "文档名对不上，实际「$DOC_NAME」"
    fi
    if grep -q '"source_url"' "$WORK/doc.json"; then
        ok "元数据里有 source_url 这个字段"
    else
        bad "元数据里没有 source_url 字段，实际：$(head -c 300 "$WORK/doc.json")"
    fi
    META_URL="$(dify_meta_value "$WORK/doc.json" source_url)"
    expect_eq "元数据 source_url 的值是原文地址" "$META_URL" "https://example.com/rd/a/$AID"
    if grep -q '"article_id"' "$WORK/doc.json"; then
        ok "元数据里有 article_id 这个字段"
    else
        bad "元数据里没有 article_id 字段（检索后鉴权就没法比对了）"
    fi
    META_AID="$(dify_meta_value "$WORK/doc.json" article_id)"
    expect_eq "元数据 article_id 的值就是推送时的文章编号" "$META_AID" "$AID"
fi

# ---------------------------------------------------------- 9
step "9. 全量同步 full_sync：不在本批的文章会被下线"
if [ "$BEFORE_ONLINE" != "0" ]; then
    echo "  跳过：跑之前库里已经有 ${BEFORE_ONLINE} 条在线文章，"
    echo "  全量下线可能误删真实数据，所以这一步不敢跑。"
else
    make_body "$WORK/p4.json" true '[
     {"article_id":@AID@,"title":"kb-sync 自测文档","file_name":"selftest.md",
      "file_url":"@FILES@/selftest.md","source_url":"https://example.com/rd/a/@AID@",
      "updated_at":"2026-09-20T12:00:00Z"}]'
    CODE=$(push "$WORK/p4.json" "$WORK/r5.json")
    expect_eq "HTTP 状态" "$CODE" "200"
    expect_eq "本批里的文章不会被下线（offline）" "$(jget "$WORK/r5.json" data.offline)" "0"

    echo "  再推一个空批次：不在批里的文章应该被下线"
    make_body "$WORK/p5.json" true '[]'
    CODE=$(push "$WORK/p5.json" "$WORK/r6.json")
    expect_eq "HTTP 状态" "$CODE" "200"
    expect_eq "下线了 1 条" "$(jget "$WORK/r6.json" data.offline)" "1"
    curl -s -H "X-KB-Sync-Token: $TOKEN" \
        "$API/api/article_meta?article_id=$AID" -o "$WORK/meta3.json"
    expect_eq "反查变成 online=False" "$(jget "$WORK/meta3.json" data.online)" "False"

    echo "  再试一次带错误的批次：全量下线必须整体跳过（防止脏数据误删整库）"
    make_body "$WORK/p6.json" true '[{"article_id":"bad","title":"x"}]'
    CODE=$(push "$WORK/p6.json" "$WORK/r7.json")
    expect_eq "HTTP 状态" "$CODE" "200"
    expect_eq "status" "$(jget "$WORK/r7.json" status)" "partial"
    expect_eq "有错误时不执行下线（offline）" "$(jget "$WORK/r7.json" data.offline)" "0"
fi

# ---------------------------------------------------------- 10
step "10. 撤回发布（unpublished）应该把它从知识库里摘掉"
AID2=$((AID + 1))
curl -s -o "$WORK/up1.json" -X POST "$API/sync/kb/article/push" \
    -H "X-KB-Sync-Token: $TOKEN" -H 'Content-Type: application/json' \
    --data "{\"items\":[{\"article_id\":$AID2,\"title\":\"kb-sync 自测文档2\",\"file_name\":\"selftest.md\",\"file_url\":\"$FILES/selftest.md\",\"source_url\":\"https://example.com/rd/a/$AID2\",\"publish_status\":\"published\",\"updated_at\":\"2026-09-20T10:00:00Z\"}]}"
expect_eq "先按 published 推一条" "$(jget "$WORK/up1.json" data.accepted)" "1"
curl -s -H "X-KB-Sync-Token: $TOKEN" \
    "$API/api/article_meta?article_id=$AID2" -o "$WORK/up_meta1.json"
expect_eq "这条已经在库里（online=True）" "$(jget "$WORK/up_meta1.json" data.online)" "True"

curl -s -o "$WORK/up2.json" -X POST "$API/sync/kb/article/push" \
    -H "X-KB-Sync-Token: $TOKEN" -H 'Content-Type: application/json' \
    --data "{\"items\":[{\"article_id\":$AID2,\"publish_status\":\"unpublished\"}]}"
expect_eq "改推 unpublished" "$(jget "$WORK/up2.json" data.accepted)" "1"
curl -s -H "X-KB-Sync-Token: $TOKEN" \
    "$API/api/article_meta?article_id=$AID2" -o "$WORK/up_meta2.json"
expect_eq "撤回后变成 online=False" "$(jget "$WORK/up_meta2.json" data.online)" "False"

# ---------------------------------------------------------- 11
step "11. 收尾：把测试文章删掉"
curl -s -o "$WORK/del.json" -X POST "$API/sync/kb/article/push" \
    -H "X-KB-Sync-Token: $TOKEN" -H 'Content-Type: application/json' \
    --data "{\"items\":[{\"article_id\":$AID,\"publish_status\":\"deleted\"}]}"
expect_eq "删除请求的 status" "$(jget "$WORK/del.json" status)" "success"
sleep 2
expect_eq "在线文章数恢复成跑之前的 ${BEFORE_ONLINE} 条" "$(online_count)" "$BEFORE_ONLINE"
expect_eq "Dify 文档数恢复成跑之前的 ${BEFORE_DOCS} 个" "$(dify_doc_count)" "$BEFORE_DOCS"
curl -s -H "X-KB-Sync-Token: $TOKEN" \
    "$API/api/article_meta?article_id=$AID" -o "$WORK/meta4.json"
expect_eq "映射表里这条已标记下线" "$(jget "$WORK/meta4.json" data.online)" "False"

# ---------------------------------------------------------- 汇总
printf '\n================== 汇总 ==================\n'
echo "通过 $PASS 项，失败 $FAIL 项"
if [ "$FAIL" -eq 0 ]; then
    echo "全部通过，服务是好的。"
    echo "临时文件已清理（$WORK），测试用的假文章也删干净了。"
else
    echo "有失败项，把上面这一整段输出发给我。"
fi
echo "服务日志：$DIR/logs/app.log"
[ "$FAIL" -eq 0 ] || exit 1
