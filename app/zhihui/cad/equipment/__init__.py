"""化工设备图的确定性生成器（任务书第 5 节）：参数 → 查标准表 → 部件 → 视图 → 尺寸 → 件号 → 表格 → 布局。

这个包里只有确定性代码（不调用大模型）；几何原语复用 zhihui/cad/geom 和 zhihui/cad/toolsets/common.py 的 ops 格式。
"""
