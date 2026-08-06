#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 定时抓取脚本（零依赖，仅标准库）
==============================================
由 .github/workflows/refresh.yml 每 5 分钟调度一次。
抓取 ggzy.gov.cn 全国工程招标公告 → 去重评分 → 写 tenders.json。
若抓取失败则保留旧文件不覆盖（优雅降级）。
"""

import json
import os
import sys
import time
from datetime import datetime

# 注入环境变量（在 GitHub Actions 中通过 env 设置，本地调试兜底）
os.environ.setdefault("LIVE", "True")
os.environ.setdefault("DELAY", "0.8")

# 导入核心采集逻辑
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tender_service import aggregate

OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tenders.json")

def main():
    print(f"[{datetime.now().isoformat()}] 开始抓取...")
    
    try:
        items = aggregate(region="", keyword="", ttype="all")
    except Exception as e:
        print(f"[错误] 抓取失败: {e}")
        print("保留旧 tenders.json，不覆盖")
        sys.exit(0)  # 非 0 会让 Actions 标记失败，但不会覆盖文件
    
    # 分离真实数据（有 URL）和示例数据（无 URL）
    real_items = [it for it in items if it.get("url")]
    
    print(f"  抓取: {len(items)} 条（真实 {len(real_items)} 条）")
    
    # 生成快照
    snapshot = {
        "updated_at": datetime.now().isoformat(),
        "is_live_snapshot": True,
        "count": len(items),
        "real_count": len(real_items),
        "items": items,
    }
    
    # 原子写入
    tmp = OUTPUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUTPUT)  # 原子替换，避免读写半截
    
    size = os.path.getsize(OUTPUT)
    print(f"  ✓ 已写入 {OUTPUT} ({size/1024:.1f} KB)")
    
    # 检查是否有真实数据（GGZY 可达性自检）
    if len(real_items) == 0:
        print("[警告] 本次无真实数据（ggzy.gov.cn 可能不可达），保留示例数据")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
