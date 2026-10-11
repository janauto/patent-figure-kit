"""能力探测（WP2）：导出包能出哪些格式、AI 通道是否可用——事前告诉前端，不让用户点了才失败。

``GET /v1/capabilities`` 与导出服务共用这里的判定：DWG 转换器不可用时，能力接口
``export.dwg_available=false``（前端事前禁选），导出清单里 DWG 一律 ``skip`` 并给原因。
OCP 无关，只做本机探测（文件存在 / PATH / 可选包能否导入）。
"""

from __future__ import annotations

import shutil
from typing import Any, Dict, Optional

from patent_figure import deliverable as _D

#: 开发期显示名（架构 v1 §1.1：常量化，改名只改这里 + 前端 brand.js）。
DISPLAY_NAME = "墨线"

#: 外发字段（F9）：AI 起名时发给大模型通道的内容类别，前端据此在外发前明示。
AI_OUTBOUND_FIELDS = ["part_names", "part_codes"]
AI_OUTBOUND_NOTE = ("AI 起名会把选中零件的 STEP 零件名（常含内部件号）、包围盒尺寸与装配路径发给"
                    "所配置的大模型通道；几何模型本身不出本机。")


def dwg_route() -> Optional[str]:
    """``"autocad"`` (AcCoreConsole found), ``"libredwg"`` (dwgwrite + dwgread on PATH) or None."""
    try:
        import doctor  # scripts/ is on sys.path for every entry point that exports
        if doctor._expand_all(doctor.AUTOCAD_CORE_PATTERNS):
            return "autocad"
    except Exception:
        pass
    if shutil.which("dwgwrite") and shutil.which("dwgread"):
        return "libredwg"
    return None


def export_capabilities() -> Dict[str, Any]:
    from patent_figure import deliverable_check as _C
    route = dwg_route()
    docx_ok = _D.docx_available()
    pdf_check = _C.pdf_checker_available()
    return {
        "formats": ["pdf", "docx", "dxf", "dwg"],
        "primary_formats": ["pdf", "docx"],
        "source_formats": ["dxf", "dwg"],
        "variants": {"filing": "递交版（仅图号）", "disclosure": "交底版（图号 + 件号表）",
                     "both": "两版"},
        "pdf_available": True,
        "pdf_check_available": pdf_check,
        "docx_available": docx_ok,
        "dwg_available": route is not None,
        "dwg_route": route,
        "unavailable_reasons": {
            k: v for k, v in (
                ("dwg", None if route else "未检测到 DWG 转换器（AutoCAD Core Console 或 LibreDWG）"),
                ("docx", None if docx_ok else "未安装 python-docx（pip install python-docx）"),
                ("pdf", None if pdf_check else
                 "未安装 pypdf：PDF 能生成但无法逐页核验，按规则会报不通过（pip install pypdf）"),
            ) if v},
        "sheet_presets": {k: {"area_mm": list(v["area_mm"]), "label": v["label"]}
                          for k, v in _D.SHEET_PRESETS.items()},
        "default_preset": _D.DEFAULT_PRESET,
        "dpi": {"default": _D.DEFAULT_DPI, "min": _D.DPI_MIN, "max": _D.DPI_MAX},
        "page": {"size_mm": list(_D.PAGE_MM), "margins_mm": dict(_D.MARGINS_MM)},
        "min_text_mm": _D.MIN_TEXT_MM,
        "min_text_px": _D.MIN_TEXT_PX,
        "note": _D.CNIPA_NOTE,
        "files": ["说明书附图_递交版.pdf", "说明书附图_递交版.docx", "说明书附图_交底版.pdf",
                  "说明书附图_交底版.docx", "附图说明.txt", "核对清单.txt", "核验报告.json"],
    }


def ai_capabilities(public_settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    s = dict(public_settings or {})
    provider = s.get("provider") or "none"
    return {"configured": provider not in (None, "", "none"), "provider": provider,
            "provider_label": s.get("provider_label", ""),
            "outbound_fields": list(AI_OUTBOUND_FIELDS), "outbound_note": AI_OUTBOUND_NOTE}
