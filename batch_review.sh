#!/bin/bash
# 批量评分（数据闭环）：逐行读取评分文件 → 调 memory-trace-review
# 评分文件每行：ID extraction decision confidence provenance privacy （空格分隔，1~5）
# 用法：bash batch_review.sh 评分结果.txt
cd "$(dirname "$0")"
while read -r id e d c p pr; do
    [ -z "$id" ] && continue
    ./venv/bin/python tools.py memory-trace-review "$id" \
        --extraction "$e" --decision "$d" --confidence "$c" --provenance "$p" --privacy "$pr" || echo "评分失败: $id"
done < "$1"
echo "批量评分完成"
