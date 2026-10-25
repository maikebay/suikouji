"""随口记 · 经营记账引擎。

分层：
    parser    中文口语 → 账目结构
    sheet     表格文件 → 二维数组
    importer  二维数组 → 账目/销量（带列映射）
    store     落库（Store 接口 + SQLite 实现，云端实现同接口）
    cost      商品—原料—配方 → 单品成本
    reconcile 理论 vs 实际 → 待确认差异
    report    报表渲染（Markdown）
    cli       本地命令行入口
"""
