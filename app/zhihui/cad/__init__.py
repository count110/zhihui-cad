"""CAD 引擎：图元库、布局、布线、DXF 读写、渲染、校验、差异。"""

import logging

# ezdxf 在 INFO 级别会逐条报告写文件/渲染细节，MCP Server 的日志里全是它，降到 WARNING
logging.getLogger("ezdxf").setLevel(logging.WARNING)
