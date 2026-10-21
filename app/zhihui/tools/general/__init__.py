"""通用 CAD 函数（45 个预置函数 + 6 个感知函数 + 系统函数）。导入本包即完成注册。"""
from . import env_tools, draw_tools, view_tools, edit_tools, system_tools  # noqa: F401
from . import annot_tools, layout_tools, block_tools, constraint_tools, solid_tools, spec_tools  # noqa: F401
from . import vessel_tools  # noqa: F401  化工设备图（chem_equipment）
from .registry import REGISTRY, catalog, mcp_function, register_mcp, run_tool, tools_of  # noqa: F401
