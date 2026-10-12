"""工作台的 MCP 端点（Streamable HTTP，JSON 应答；另附 stdio 入口）。

千问办公等 MCP 客户端按 URL 挂载：``http://127.0.0.1:<port>/mcp``，带
``Authorization: Bearer <token>``（或 ``?token=``）。手写一个最小实现而不用官方 SDK，
是因为出图工具链跑在系统 Python 3.9 上，SDK 要求 3.10+。

工具的分工与 skill 的原则一致：客户端里的大模型可以**自己当起草者**——
``get_project_parts`` 读零件清单 → ``update_terms`` 写中文名 → ``render_project`` 出图，
编号、版面、件号表仍全部由脚本完成；也可以让工作台调 DeepSeek 代劳（``ai_*``）。
"""

from __future__ import annotations

import base64
import json
from typing import Any, Callable, Dict, List, Optional

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "patent-figure-workbench", "title": "专利附图工作台", "version": "1.0.0"}
INSTRUCTIONS = (
    "专利附图工作台：把 3D 装配体（STEP）做成带附图标记与件号名称表的专利附图，"
    "也能把方法描述画成带 S101 步骤号的流程图。规则：附图标记号、步骤号、坐标都由程序发放，"
    "你只写中文零件名称和流程语义。典型顺序：list_projects → get_project_parts → update_terms "
    "→ render_project → get_project_figures；流程图用 get_flowchart_guide 看格式后 render_flowchart。"
)


def _schema(props: Dict[str, Any], required: List[str] = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required),
            "additionalProperties": False}


PID = {"type": "string", "description": "工程 id（list_projects 返回）"}
TOOLS = [
    {"name": "list_projects", "title": "列出工程",
     "description": "列出工作台里的全部工程（含示例工程）：id、名称、状态、附图数、流程图数。",
     "inputSchema": _schema({})},
    {"name": "get_project_parts", "title": "读取零件清单",
     "description": "读取某工程的 3D 零件清单：STEP 零件名、包围盒尺寸、装配路径、当前中文名。"
                    "用于给零件起专利附图名称。only_unnamed=true 只返回还没起名的。",
     "inputSchema": _schema({"project_id": PID,
                             "only_unnamed": {"type": "boolean", "default": True}},
                            ["project_id"])},
    {"name": "update_terms", "title": "写入零件中文名",
     "description": "把零件中文技术名词写进工程计划。selector 用零件名原样；term 只能是中文名词，"
                    "不得含数字/件号/英文；标准件 label 写 none。默认不覆盖人已填的名字。",
     "inputSchema": _schema({
         "project_id": PID,
         "terms": {"type": "array", "items": _schema({
             "selector": {"type": "string"}, "term": {"type": "string"},
             "label": {"type": "string", "enum": ["once", "all", "none"]}},
             ["selector", "term"])},
         "overwrite": {"type": "boolean", "default": False}}, ["project_id", "terms"])},
    {"name": "render_project", "title": "出图",
     "description": "对工程跑一次完整出图：校验 → 消隐出图 → 质量闸门 → 生成交底版（图号 + 件号名称表）"
                    "与递交版（仅图号）。可能需要数分钟。返回每张图是否通过 QA 与修改提示。",
     "inputSchema": _schema({"project_id": PID}, ["project_id"])},
    {"name": "get_project_figures", "title": "取附图结果",
     "description": "取工程最近一次出图结果：每张图的图号、件号表、附图说明、附图标记说明、文件链接；"
                    "include_images=true 时附带交底版 PNG 预览。",
     "inputSchema": _schema({"project_id": PID,
                             "include_images": {"type": "boolean", "default": False}},
                            ["project_id"])},
    {"name": "ai_draft_terms", "title": "AI 起草零件名",
     "description": "让工作台调用已配置的大模型给还没命名的零件起中文名（每条带 confidence/reason），"
                    "只填空白、不覆盖人填的。selectors 限定零件（缺省=全部空白）；apply 缺省 true 时写入"
                    "计划，false 只返回建议。没配大模型通道时返回 status=ai_not_configured。外发前请留意"
                    "返回的 outbound_fields（零件名 / 件号会发给大模型）。",
     "inputSchema": _schema({"project_id": PID,
                             "selectors": {"type": "array", "items": {"type": "string"}},
                             "apply": {"type": "boolean", "default": True}}, ["project_id"])},
    {"name": "get_flowchart_guide", "title": "流程图格式说明",
     "description": "返回流程图语义 JSON（patent-flowchart/1）的格式与规则，写 render_flowchart 的 spec 前先读。",
     "inputSchema": _schema({})},
    {"name": "render_flowchart", "title": "画流程图",
     "description": "把流程图语义 JSON 画成专利附图（黑白、S101 步骤号由程序发放）。"
                    "给 project_id 则存进该工程并按工程续编图号，否则单独出图。返回步骤号对照与 PNG。",
     "inputSchema": _schema({"spec": {"type": "object"}, "project_id": PID,
                             "figure_number": {"type": "integer", "minimum": 1}}, ["spec"])},
    {"name": "generate_flowchart", "title": "文字生成流程图",
     "description": "把一段方法步骤描述交给 DeepSeek 整理成流程图并出图（自动校验、修复一轮）。",
     "inputSchema": _schema({"text": {"type": "string"}, "title": {"type": "string"},
                             "project_id": PID}, ["text"])},
    {"name": "submit_render", "title": "异步出图",
     "description": "对工程发起一次异步出图，立即返回 job_id（不阻塞）。用 get_job 轮询到 "
                    "succeeded/failed，再用 get_project_figures 取结果。适合大装配体出图较久时。",
     "inputSchema": _schema({"project_id": PID}, ["project_id"])},
    {"name": "get_job", "title": "查询作业",
     "description": "按 job_id 查作业状态：queued/running/succeeded/failed/cancelled/interrupted，"
                    "含阶段、进度、日志与结果/错误。submit_render、get_export 返回的 job 都用它查。",
     "inputSchema": _schema({"job_id": {"type": "string", "description": "submit_render/get_export 返回的 id"}},
                            ["job_id"])},
    {"name": "cancel_job", "title": "取消作业",
     "description": "取消一个还没结束的作业（已结束则原样返回）。",
     "inputSchema": _schema({"job_id": {"type": "string"}}, ["job_id"])},
    {"name": "get_export", "title": "导出附图包（异步）",
     "description": "对工程发起一次导出，返回 export_id（= job_id）。用 get_job 轮询到 succeeded，"
                    "结果里的 download_url 即 zip 下载地址。PDF/Word 是主交付（A4、逐页核验页幅/页边/"
                    "缩放后字高/图号/页数，不过的移到 _未通过核验/ 并在 items 里给原因），DXF/DWG 是源文件；"
                    "DWG 无转换器时为 skip。包内附 附图说明.txt 与 核对清单.txt。",
     "inputSchema": _schema({"project_id": PID,
                             "formats": {"type": "array", "items": {"type": "string",
                                         "enum": ["pdf", "docx", "dxf", "dwg"]}},
                             "variant": {"type": "string",
                                         "enum": ["filing", "disclosure", "both"]},
                             "dpi": {"type": "integer", "minimum": 72, "maximum": 600,
                                     "default": 300},
                             "sheet_preset": {"type": "string",
                                              "enum": ["standard", "conservative"]}},
                            ["project_id"])},
    {"name": "suggest_figures", "title": "候选分图（异步）",
     "description": "给工程提若干分图候选（整机装配图、沿主轴分解图、每个子装配/同轴组一张分解图），"
                    "每个候选都实际渲染并过质量闸门后才有 pass/fail 与原因。返回 job_id，用 get_job "
                    "轮询；result.candidates[].members 是真实零件名。标准件默认不发标记。",
     "inputSchema": _schema({"project_id": PID,
                             "label_standard_parts": {"type": "boolean", "default": False},
                             "max_candidates": {"type": "integer", "minimum": 1,
                                                "maximum": 16}}, ["project_id"])},
    {"name": "accept_figure_candidates", "title": "接受候选分图",
     "description": "把选中的候选（candidate_ids）写进工程计划：成员先在当前装配体上解析，解析不了"
                    "就整体拒绝、不写入。mode=append 追加（默认）/ replace 替换全部分图。",
     "inputSchema": _schema({"project_id": PID, "job_id": {"type": "string"},
                             "candidate_ids": {"type": "array", "items": {"type": "string"}},
                             "mode": {"type": "string", "enum": ["append", "replace"]},
                             "apply_standard_labels": {"type": "boolean", "default": True}},
                            ["project_id", "candidate_ids"])},
]

FLOW_GUIDE = {
    "schema": "patent-flowchart/1",
    "format": {"schema": "patent-flowchart/1", "title": "××方法的流程图",
               "step_style": "S101 或 S1（可省略，默认 S101）",
               "nodes": [{"id": "英文短 id", "kind": "start|process|decision|end|io",
                          "text": "中文，≤30 字"}],
               "edges": [{"from": "id", "to": "id", "label": "判断出线写 是/否"}]},
    "rules": ["不写步骤号、坐标、尺寸——程序按流程顺序发 S101、S102…",
              "decision 正好两条出线，label 为「是」「否」；其他节点最多一条出线",
              "回到前面步骤的循环直接画一条指回去的边",
              "最多一个 start；end 不能有出线；节点只能有 id/kind/text 三个字段"],
    "example": {"schema": "patent-flowchart/1", "title": "箱体温度控制方法的流程图",
                "nodes": [{"id": "a", "kind": "start", "text": "开始"},
                          {"id": "b", "kind": "process", "text": "采集箱体内部温度"},
                          {"id": "c", "kind": "decision", "text": "温度是否高于设定值？"},
                          {"id": "d", "kind": "process", "text": "启动风扇降温"},
                          {"id": "e", "kind": "end", "text": "结束"}],
                "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"},
                          {"from": "c", "to": "d", "label": "是"},
                          {"from": "c", "to": "b", "label": "否"},
                          {"from": "d", "to": "e"}]},
}


class ToolError(Exception):
    pass


def _ok(data: Any, images: Optional[List[bytes]] = None) -> dict:
    content = [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=1)}]
    for png in images or []:
        content.append({"type": "image", "mimeType": "image/png",
                        "data": base64.b64encode(png).decode("ascii")})
    out = {"content": content, "isError": False}
    if isinstance(data, dict):
        out["structuredContent"] = data
    return out


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def handle_message(msg: Any, tools: Dict[str, Callable[[dict], Any]]) -> Optional[dict]:
    """处理一条 JSON-RPC 消息；通知返回 None。"""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:      # 通知（initialized / cancelled 等）
        return None

    def result(r):
        return {"jsonrpc": "2.0", "id": mid, "result": r}

    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return result({"protocolVersion": version,
                       "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS})
    if method == "ping":
        return result({})
    if method == "tools/list":
        return result({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = tools.get(name)
        if fn is None:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": "Unknown tool: %s" % name}}
        try:
            out = fn(args)
        except ToolError as exc:
            return result(_err(str(exc)))
        except Exception as exc:  # 工具内部异常回给客户端，不让会话断掉
            return result(_err("工具执行出错：%s" % exc))
        if isinstance(out, tuple):
            return result(_ok(out[0], out[1]))
        return result(_ok(out))
    if method in ("resources/list", "prompts/list"):
        return result({method.split("/")[0]: []})
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": "Method not found: %s" % method}}


def handle_payload(payload: Any, tools: Dict[str, Callable[[dict], Any]]):
    """单条或批量；全是通知时返回 None（HTTP 202）。"""
    if isinstance(payload, list):
        out = [r for r in (handle_message(m, tools) for m in payload) if r is not None]
        return out or None
    return handle_message(payload, tools)
