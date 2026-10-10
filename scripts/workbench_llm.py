"""工作台的大模型通道：DeepSeek API（主）/ 本机 CodeBuddy CLI（备用）。

大模型在这条流水线里只做两件「写语义」的事，产物都要过脚本校验：

* ``draft_terms``      —— 给零件起中文技术名词（写进 figure-plan.json 的 terms[].term）；
* ``flowchart_from_text`` —— 把一段方法描述整理成流程图语义 JSON（patent-flowchart/1）。

附图标记、步骤号、坐标、版面一概不让它写：提示词里明说，返回值里有也会被丢弃，
校验器还会再拦一道。密钥只从环境变量或 0600 权限的本机配置文件读，绝不回显、不写日志。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_DIR = Path(os.environ.get("PATENT_WORKBENCH_CONFIG",
                                  str(Path.home() / ".config" / "patent-workbench")))
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
CODEBUDDY_CANDIDATES = (
    "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy",
    "codebuddy",
)
CODEBUDDY_MODEL = "deepseek-v3-2-volc"
TIMEOUT_S = 300
MAX_OUTPUT_TOKENS = 64000


class LLMError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 配置（密钥只在这里读写）                                                        #
# --------------------------------------------------------------------------- #

def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    tmp = CONFIG_FILE.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)
    os.chmod(CONFIG_FILE, 0o600)


def _codebuddy_path() -> Optional[str]:
    for cand in CODEBUDDY_CANDIDATES:
        p = cand if os.path.isabs(cand) else shutil.which(cand)
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def settings(cfg: Optional[dict] = None) -> dict:
    """解析出当前生效的通道设置（含密钥，只供进程内使用）。"""
    cfg = cfg if cfg is not None else load_config()
    ds = cfg.get("deepseek", {})
    key = os.environ.get("DEEPSEEK_API_KEY") or ds.get("api_key") or ""
    provider = cfg.get("provider") or "auto"
    if provider == "auto":
        provider = "deepseek" if key else ("codebuddy" if _codebuddy_path() else "none")
    return {
        "provider": provider,
        "api_key": key,
        "key_source": "环境变量 DEEPSEEK_API_KEY" if os.environ.get("DEEPSEEK_API_KEY")
        else ("本机配置文件" if ds.get("api_key") else ""),
        "base_url": (os.environ.get("DEEPSEEK_BASE_URL") or ds.get("base_url")
                     or DEFAULT_BASE_URL).rstrip("/"),
        "model": os.environ.get("DEEPSEEK_MODEL") or ds.get("model") or DEFAULT_MODEL,
        "codebuddy": _codebuddy_path(),
    }


def public_settings() -> dict:
    """给前端看的版本：密钥只露是否已配置与末 4 位。"""
    s = settings()
    key = s.pop("api_key")
    s["api_key_set"] = bool(key)
    s["api_key_hint"] = ("…" + key[-4:]) if len(key) >= 8 else ""
    s["provider_label"] = {
        "deepseek": "DeepSeek API（%s）" % s["model"],
        "codebuddy": "本机 CodeBuddy · DeepSeek V3.2（备用通道，未配置 API 密钥）",
        "none": "未配置——请在设置里填 DeepSeek API 密钥",
    }.get(s["provider"], s["provider"])
    s["configured_provider"] = load_config().get("provider") or "auto"
    return s


# --------------------------------------------------------------------------- #
# 调用                                                                          #
# --------------------------------------------------------------------------- #

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _parse_json(text: str) -> Any:
    text = text.strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0] or [-1])
        if start < 0:
            raise LLMError("模型没有返回 JSON")
        end = max(text.rfind("}"), text.rfind("]"))
        try:
            return json.loads(text[start:end + 1])
        except ValueError as exc:
            raise LLMError("模型返回的 JSON 无法解析：%s" % exc)


def chat_json(system: str, user: str, *, max_tokens: int = 12000,
              cfg: Optional[dict] = None) -> Any:
    s = settings(cfg)
    if s["provider"] == "deepseek":
        if not s["api_key"]:
            raise LLMError("没有配置 DeepSeek API 密钥")
        import httpx
        body = {
            "model": s["model"],
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": False,
        }
        last = None
        repaired = False
        limit = max_tokens
        for attempt in range(4):
            body["max_tokens"] = limit
            try:
                r = httpx.post(s["base_url"] + "/chat/completions", json=body, timeout=TIMEOUT_S,
                               headers={"Authorization": "Bearer " + s["api_key"]})
            except httpx.HTTPError as exc:
                last = "网络错误：%s" % type(exc).__name__
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503):
                last = "DeepSeek 返回 %d" % r.status_code
                time.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code == 401:
                raise LLMError("DeepSeek 拒绝了密钥（401）——请在设置里核对")
            if r.status_code >= 400:
                raise LLMError("DeepSeek 返回 %d：%s" % (r.status_code, r.text[:300]))
            choice = r.json()["choices"][0]
            content = (choice.get("message") or {}).get("content") or ""
            if not content.strip() and choice.get("finish_reason") == "length":
                # 推理模型（如 deepseek-v4-pro）的思考过程也占输出额度：额度耗尽时正文为空。
                if limit >= MAX_OUTPUT_TOKENS:
                    raise LLMError("模型推理用完了 %d 输出额度仍未给出结果——可在设置里换成更快的模型"
                                   "（如 deepseek-v4-flash / deepseek-chat）" % limit)
                limit = min(MAX_OUTPUT_TOKENS, limit * 3)
                last = "输出额度不足，已放大到 %d 重试" % limit
                continue
            try:
                return _parse_json(content)
            except LLMError as exc:
                if repaired:
                    raise
                repaired = True   # 只修一次：把解析错误原样回给模型，让它重出完整 JSON
                body["messages"] = body["messages"][:2] + [
                    {"role": "assistant", "content": content[:20000]},
                    {"role": "user", "content": "上面的输出不是合法 JSON（%s）。请重新输出完整、合法的 JSON，"
                                                "不要省略任何内容，不要解释。" % exc}]
                last = str(exc)
                continue
        raise LLMError(last or "DeepSeek 调用失败")
    if s["provider"] == "codebuddy":
        exe = s["codebuddy"]
        if not exe:
            raise LLMError("没有找到本机 CodeBuddy CLI")
        prompt = (system + "\n\n" + user +
                  "\n\n只输出一个 JSON 对象，不要解释，不要使用任何工具。")
        work = CONFIG_DIR / "codebuddy-cwd"
        work.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run([exe, "-p", "--model", CODEBUDDY_MODEL, "--tools", "Read",
                                   prompt], capture_output=True, text=True,
                                  timeout=TIMEOUT_S, cwd=str(work))
        except subprocess.TimeoutExpired:
            raise LLMError("CodeBuddy 超时（%ds）" % TIMEOUT_S)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise LLMError("CodeBuddy 调用失败（exit=%d）：%s"
                           % (proc.returncode, (proc.stderr or "无输出")[-300:]))
        return _parse_json(proc.stdout)
    raise LLMError("没有可用的大模型通道：请在设置里填 DeepSeek API 密钥")


# --------------------------------------------------------------------------- #
# 任务一：零件术语                                                              #
# --------------------------------------------------------------------------- #

TERMS_SYSTEM = """你是中国专利代理人，正在为机械/电子产品的专利附图给零件起「附图标记名称」。
规则（违反任何一条，结果会被校验器整条丢弃）：
1. term 只写中文技术名词，如「底壳」「行星齿轮减速箱」「第一回转轴承」；
2. 绝不出现数字编号、附图标记号、厂内件号、型号（如 24BYJ48、PCB-K、BREP_1）、英文缩写；
3. 同类多件用「第一/第二」区分，不用阿拉伯数字；
4. 螺钉、垫片、连接器、端子、泡棉、电容电阻等标准件或微小件 label 设为 "none"（不标注），但 term 仍要写中文名（如「连接器」「缓冲泡棉」「螺钉」），不得留空；
4.1 不同零件不得同名：同类件用「第一电路板」「第二电路板」区分；
4.2 同一个零件名（instances>1）的多个实例共用一个名称，写「阀座」而不是「第一阀座、第二阀座」；
5. 判断不了是什么的零件，term 写你最有把握的上位名称（如「壳体」「支架」），confidence 填 low；
6. 优先使用「术语库」里已有的叫法——同一产品的已递交专利用过的名字要一致。
只输出 JSON：{"terms":[{"selector":"零件名原样","term":"中文名","label":"once|none","confidence":"high|medium|low","reason":"一句话依据"}]}"""


def _part_brief(p: dict) -> dict:
    size = p.get("bbox_size") or []
    return {"name": p["name"], "instances": p.get("instances", 1),
            "size_mm": [round(float(v), 1) for v in size],
            "path": (p.get("path_sample") or "")[-120:],
            "degenerate": bool(p.get("degenerate"))}


def draft_terms(assembly: dict, plan: dict, *, glossary: Optional[Dict[str, str]] = None,
                bom: Optional[Dict[str, str]] = None, only_empty: bool = True,
                cfg: Optional[dict] = None) -> List[dict]:
    parts = [p for p in assembly.get("parts", []) if not p.get("degenerate")]
    have = {t.get("selector"): (t.get("term") or "").strip() for t in plan.get("terms", [])}
    todo = [p for p in parts if not (only_empty and have.get(p["name"]))]
    if not todo:
        return []
    user = {
        "产品零件（STEP 零件名、包围盒、装配路径）": [_part_brief(p) for p in todo][:160],
        "已定名的零件（参考，不要改）": {k: v for k, v in have.items() if v},
        "术语库（已递交专利用过的名字）": glossary or {},
        "BOM 匹配到的物料名称": bom or {},
    }
    out = chat_json(TERMS_SYSTEM, json.dumps(user, ensure_ascii=False), max_tokens=16000, cfg=cfg)
    rows = out.get("terms", []) if isinstance(out, dict) else []
    names = {p["name"] for p in todo}
    size = {p["name"]: max([float(v) for v in (p.get("bbox_size") or [0])]) for p in todo}
    clean = []
    for r in rows:
        if not isinstance(r, dict) or r.get("selector") not in names:
            continue
        term = str(r.get("term", "")).strip()
        if re.search(r"[、，,/；;]", term):    # 「第一阀座、第二阀座」→「阀座」：一个零件名只能有一个名称
            parts_ = [re.sub(r"^第[一二三四五六七八九十]+", "", x).strip()
                      for x in re.split(r"[、，,/；;]", term) if x.strip()]
            term = parts_[0] if parts_ and len(set(parts_)) == 1 else (parts_[0] if parts_ else "")
            r = dict(r, confidence="low",
                     reason=("模型给了多个名称，已收为一个；" + str(r.get("reason", "")))[:80])
        if not term or re.search(r"[0-9A-Za-z_]", term):
            continue                       # 数字、件号、英文一律不收
        clean.append({"selector": r["selector"], "term": term,
                      "label": "none" if r.get("label") == "none" else "once",
                      "confidence": r.get("confidence", "medium"),
                      "reason": str(r.get("reason", ""))[:80]})
    return _dedupe(clean, size, set(v for v in have.values() if v))


ORDINALS = "一二三四五六七八九十"


def _dedupe(rows: List[dict], size: Dict[str, float], taken: set) -> List[dict]:
    """不同零件同名 → 按尺寸从大到小改成「第一X / 第二X…」并降为待核对。

    专利附图里两个不同零件不能共用一个名称（会被理解为同一构件）。改名是确定性的，
    但叫法是否妥当要人看，所以统一标 low。"""
    groups: Dict[str, List[dict]] = {}
    for r in rows:
        groups.setdefault(r["term"], []).append(r)
    for term, grp in groups.items():
        if len(grp) < 2 and term not in taken:
            continue
        if term.startswith("第") and len(grp) < 2:
            continue
        grp.sort(key=lambda r: -size.get(r["selector"], 0.0))
        start = 2 if term in taken else 1          # 人工已占用原名：那件隐含为「第一」
        while ("第%s%s" % (ORDINALS[start - 1], term)) in taken and start < len(ORDINALS):
            start += 1
        for k, r in enumerate(grp):
            idx = start + k - 1
            if idx < len(ORDINALS):
                r["term"] = "第%s%s" % (ORDINALS[idx], term)
            r["confidence"] = "low"
            r["reason"] = ("与其他零件同名，已按尺寸自动区分；" + r["reason"])[:80]
    return rows


# --------------------------------------------------------------------------- #
# 任务二：流程图                                                                #
# --------------------------------------------------------------------------- #

FLOW_SYSTEM = """你是中国专利代理人，把一段方法描述整理成专利说明书附图用的流程图语义 JSON。
只输出 JSON，格式严格如下（不得增加字段）：
{"schema":"patent-flowchart/1","title":"××方法的流程图","nodes":[{"id":"英文短id","kind":"start|process|decision|end|io","text":"中文"}],"edges":[{"from":"id","to":"id","label":"是|否"}]}
规则：
1. 节点文字是动作或判断，简洁（每框 ≤ 30 字），判断节点以「？」结尾；
2. 绝不写步骤号（S101、S1、步骤一等），步骤号由程序发放；不写坐标、尺寸；
3. decision 节点正好两条出线，label 分别为「是」「否」；其余节点最多一条出线；
4. 有开始就只有一个 start；回到前面步骤的循环用一条指回去的边表示；
5. 不编造原文没有的步骤。"""


def flowchart_from_text(text: str, title: str = "", *, cfg: Optional[dict] = None,
                        validate=None) -> dict:
    user = "方法描述：\n%s\n\n%s" % (text.strip(), ("图名：" + title) if title else "")
    spec = chat_json(FLOW_SYSTEM, user, max_tokens=12000, cfg=cfg)
    if not isinstance(spec, dict):
        raise LLMError("模型返回的不是 JSON 对象")
    spec = _clean_flow(spec, title)
    if validate:
        issues = [i for i in validate(spec) if i["severity"] == "error"]
        if issues:   # 一轮修复：把校验器的原话交回给模型
            fix = ("上一版有这些错误，请只改错处、输出完整 JSON：\n" +
                   "\n".join("- %s: %s %s" % (i["code"], i["message"], i.get("hint", ""))
                             for i in issues) +
                   "\n\n上一版：\n" + json.dumps(spec, ensure_ascii=False))
            spec = _clean_flow(chat_json(FLOW_SYSTEM, user + "\n\n" + fix, cfg=cfg), title)
    return spec


def _clean_flow(spec: dict, title: str) -> dict:
    nodes = []
    for n in spec.get("nodes", []) or []:
        if isinstance(n, dict):
            txt = re.sub(r"^\s*(?:[Ss]\d+|步骤\s*\d+)[\s:：、.]*", "", str(n.get("text", "")))
            nodes.append({"id": str(n.get("id", "")), "kind": n.get("kind", "process"),
                          "text": txt.strip()})
    edges = []
    for e in spec.get("edges", []) or []:
        if isinstance(e, dict):
            row = {"from": str(e.get("from", "")), "to": str(e.get("to", ""))}
            if str(e.get("label", "") or "").strip():
                row["label"] = str(e["label"]).strip()
            edges.append(row)
    return {"schema": "patent-flowchart/1",
            "title": title or str(spec.get("title", "")) or "方法流程图",
            "nodes": nodes, "edges": edges}


# --------------------------------------------------------------------------- #
# 任务三：结构识别（给人看的结构组 + 零件中文显示名）                              #
# --------------------------------------------------------------------------- #

STRUCT_SYSTEM = """你是资深结构工程师兼专利代理人。下面是一台产品 3D 装配体的零件清单：零件名多是看不懂的 CAD 代号，
附有包围盒尺寸（mm）、中心坐标（mm）、装配路径、实例数，以及整机包围盒。请把零件划成用户一眼能看懂的「结构组」，
并给每个零件起一个中文显示名，方便用户在 3D 里按结构挑选、框选要申请专利的部分。
规则：
1. 结构组 3–10 个，按功能与位置划分，例如「顶盖与按键组件」「风扇散热结构」「主体外壳」「齿轮传动机构」「底座与电源模块」；
2. 每个零件必须且只能属于一个组；实在判断不了的放进「其他零件」组；
3. 每组写 role：一句话说明它在整机的什么位置、起什么作用（≤40 字）；
4. 零件中文名只用中文技术名词，不含数字编号、英文字母、件号；同类件用「第一/第二」区分；螺钉、连接器、泡棉等小件也要有名字；
5. 已有人工名称的零件沿用原名；优先采用术语库与 BOM 里的叫法；
6. 判断依据要用上尺寸与位置：例如沿主轴最高处、带孔栅的块状件可能是风扇或散热件，直径大而薄的环可能是轴承或磁环。
只输出 JSON：{"groups":[{"name":"组名","role":"一句说明","parts":["零件名原样"]}],"parts":{"零件名原样":{"name":"中文名","confidence":"high|medium|low"}}}"""


def excluded_by_plan(plan: dict):
    """plan.source.exclude 的 glob：被排除的件（工装、包装内托等）不参与结构识别与展示。"""
    import fnmatch
    globs = list(((plan or {}).get("source") or {}).get("exclude") or [])
    return lambda name: any(fnmatch.fnmatchcase(name, g) for g in globs)


def _structure_input(assembly: dict, plan: dict) -> dict:
    terms = {t.get("selector"): (t.get("term") or "").strip() for t in plan.get("terms", [])}
    skip = excluded_by_plan(plan)
    rows = []
    for p in assembly.get("parts", []):
        if p.get("degenerate") or skip(p["name"]):
            continue
        c = (p.get("centers") or [[0, 0, 0]])[0]
        rows.append({"name": p["name"], "instances": p.get("instances", 1),
                     "size_mm": [round(float(v), 1) for v in p.get("bbox_size", [])],
                     "center_mm": [round(float(v), 1) for v in c],
                     "path": (p.get("path_sample") or "")[-110:],
                     **({"人工名称": terms[p["name"]]} if terms.get(p["name"]) else {})})
    return {"整机包围盒": assembly.get("bbox"),
            "主轴": (assembly.get("principal_axis") or {}).get("vector"),
            "零件": rows}


def analyze_structure(assembly: dict, plan: dict, *, glossary: Optional[Dict[str, str]] = None,
                      bom: Optional[Dict[str, str]] = None, cfg: Optional[dict] = None) -> dict:
    data = _structure_input(assembly, plan)
    if glossary:
        data["术语库（已递交专利用过的名字）"] = glossary
    if bom:
        data["BOM 匹配到的物料名称"] = bom
    out = chat_json(STRUCT_SYSTEM, json.dumps(data, ensure_ascii=False), max_tokens=24000, cfg=cfg)
    if not isinstance(out, dict):
        raise LLMError("模型返回的不是 JSON 对象")
    return clean_structure(out, assembly, plan, source="ai",
                           provider=public_settings().get("provider_label", ""))


def clean_structure(raw: dict, assembly: dict, plan: dict, *, source: str,
                    provider: str = "") -> dict:
    """校验并补齐：每个零件恰好在一个组；名称不含数字字母；同名按尺寸区分。"""
    skip = excluded_by_plan(plan)
    parts = [p for p in assembly.get("parts", []) if not p.get("degenerate") and not skip(p["name"])]
    known = {p["name"] for p in parts}
    size = {p["name"]: float(p.get("max_dim") or 0) for p in parts}
    human = {t.get("selector"): (t.get("term") or "").strip() for t in plan.get("terms", [])}
    seen, groups = set(), []
    for g in raw.get("groups", []) or []:
        if not isinstance(g, dict):
            continue
        members = [n for n in g.get("parts", []) or [] if n in known and n not in seen]
        seen.update(members)
        name = str(g.get("name", "")).strip()[:20] or "结构组"
        if members:
            groups.append({"name": name, "role": str(g.get("role", "")).strip()[:60],
                           "parts": members})
    rest = [p["name"] for p in parts if p["name"] not in seen]
    if rest:
        other = next((g for g in groups if g["name"] == "其他零件"), None)
        if other:
            other["parts"] += rest
        else:
            groups.append({"name": "其他零件", "role": "未能可靠归组的零件，可在此逐个查看", "parts": rest})
    for i, g in enumerate(groups, start=1):
        g["id"] = "g%d" % i
        g["parts"].sort(key=lambda n: -size.get(n, 0))
    rows = []
    for n, v in (raw.get("parts") or {}).items():
        if n not in known or not isinstance(v, dict):
            continue
        nm = str(v.get("name", "")).strip()
        if human.get(n):
            nm = human[n]
        if not nm or re.search(r"[0-9A-Za-z_]", nm):
            continue
        rows.append({"selector": n, "term": nm, "label": "once",
                     "confidence": v.get("confidence", "medium"), "reason": ""})
    human_rows = [r for r in rows if human.get(r["selector"])]
    ai_rows = _dedupe([r for r in rows if not human.get(r["selector"])], size,
                      {v for v in human.values() if v})
    names = {r["selector"]: {"name": r["term"], "confidence": r["confidence"]}
             for r in human_rows + ai_rows}
    return {"schema": "patent-structure/1", "source": source, "provider": provider,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), "groups": groups, "names": names}


def fallback_structure(assembly: dict, plan: Optional[dict] = None) -> dict:
    """没有大模型时的兜底：按 STEP 装配层级的第一级子装配分组，单件归入「散件」。"""
    skip = excluded_by_plan(plan or {})
    parts = [p for p in assembly.get("parts", []) if not p.get("degenerate") and not skip(p["name"])]
    buckets: Dict[str, List[str]] = {}
    for p in parts:
        segs = (p.get("path_sample") or p["name"]).split("/")
        key = segs[1] if len(segs) > 2 else "__loose__"
        buckets.setdefault(key, []).append(p["name"])
    groups = []
    loose = buckets.pop("__loose__", [])
    for k, members in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        if len(members) == 1:
            loose += members
            continue
        groups.append({"name": "子装配 %d" % (len(groups) + 1),
                       "role": "按 STEP 装配层级自动分组（%s）" % k[:28], "parts": members})
    if loose:
        groups.append({"name": "散件", "role": "装配层级里不属于任何子装配的零件", "parts": loose})
    raw = {"groups": groups, "parts": {}}
    out = clean_structure(raw, assembly, {"terms": [], "source": (plan or {}).get("source", {})},
                          source="fallback")
    return out
