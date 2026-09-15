"""tools_inventory.py — catalog ทูล + สิทธิ์รายเอเจนต์ + ยอดใช้จริง (read-only).

ความจริงของระบบ: ทูลมากับ harness (opencode) ไม่ได้มากับโมเดล — โมเดลเป็น
engine ส่วนสิทธิ์ว่าเอเจนต์ไหนเรียกทูลอะไรได้อยู่ในไฟล์นิยามเอเจนต์
(~/.config/opencode/agent/*.md หัวข้อ permission:) สคริปต์นี้อ่านไฟล์พวกนั้น
แล้วผสานกับยอดเรียกจริงใน tool-calls.jsonl ได้เป็นเมทริกซ์
"ทูล × เอเจนต์ (อนุญาต/ใช้จริง)" ให้แดชบอร์ด
"""
import os
import re

DEFAULT_AGENTS_DIR = os.path.join(os.path.expanduser("~"), ".config", "opencode", "agent")

# (tool-key ตัวเล็ก, หมวด, คำอธิบาย)
TOOL_CATALOG = [
    ("read", "file", "อ่านไฟล์/โฟลเดอร์"),
    ("edit", "file", "แก้ไฟล์แบบ exact-match"),
    ("write", "file", "เขียน/เขียนทับไฟล์"),
    ("glob", "file", "ค้นไฟล์ตาม pattern"),
    ("bash", "exec", "รันคำสั่ง shell"),
    ("grep", "search", "ค้นเนื้อหาในไฟล์ด้วย regex"),
    ("task", "orchestration", "สปอว์น subagent"),
    ("todowrite", "orchestration", "ลิสต์งาน todo"),
    ("skill", "orchestration", "โหลด skill เสริม"),
    ("webfetch", "web", "ดึงเนื้อหาจาก URL"),
    ("websearch", "web", "ค้นเว็บเรียลไทม์"),
    ("memory_recall", "memory", "ค้นความจำ (memory-mcp)"),
    ("memory_remember", "memory", "บันทึก preference (memory-mcp)"),
    ("memory_save_lesson", "memory", "บันทึกบทเรียน (memory-mcp)"),
    ("memory_get_profile", "memory", "โปรไฟล์ผู้ใช้ย่อ (memory-mcp)"),
    ("memory_search_history", "memory", "ค้น prompt เก่า (memory-mcp)"),
    ("openvisio_resolve_context", "graph", "โครงรีโปพร้อมอันดับงาน (openvisio)"),
    ("openvisio_search_code", "graph", "ค้นโค้ด indexed (openvisio)"),
    ("openvisio_find_symbol", "graph", "หาสัญลักษณ์ตามชื่อ/ภาษาธรรมชาติ (openvisio)"),
    ("openvisio_trace_calls", "graph", "ไล่ call graph (openvisio)"),
    ("openvisio_get_dependents", "graph", "วิเคราะห์ import impact (openvisio)"),
    ("openvisio_get_neighborhood", "graph", "ซับกราฟรอบไฟล์ (openvisio)"),
    ("openvisio_get_hotspots", "graph", "ไฟล์เสี่ยง/สำคัญ (openvisio)"),
    ("openvisio_get_repo_skeleton", "graph", "แผนที่รีโปทั้งชุด (openvisio)"),
]
CATALOG_ORDER = {name: i for i, (name, _, _) in enumerate(TOOL_CATALOG)}
CATALOG_DESC = {name: (cat, desc) for name, cat, desc in TOOL_CATALOG}


def normalize_tool(name):
    return str(name or "?").strip().lower()


def categorize(name):
    key = normalize_tool(name)
    if key in CATALOG_DESC:
        return CATALOG_DESC[key][0]
    if key.startswith("memory_"):
        return "memory"
    if key.startswith("openvisio_"):
        return "graph"
    return "observed"


def parse_agent_permissions(agents_dir=None):
    """อ่าน permission: ของไฟล์ *.md → {agent: {model, steps, tools:{tool: allow|deny|ask|unknown}}}."""
    agents_dir = agents_dir or DEFAULT_AGENTS_DIR
    result = {}
    if not os.path.isdir(agents_dir):
        return result
    for fn in sorted(os.listdir(agents_dir)):
        if not fn.endswith(".md"):
            continue
        agent = fn[:-3]
        try:
            with open(os.path.join(agents_dir, fn), "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            continue
        model = re.search(r"^model:\s*(.+)$", text, re.M)
        steps = re.search(r"^steps:\s*(\d+)", text, re.M)
        tools = {}
        perm = re.search(r"^permission:\s*\n((?:[ \t]+\w+:.*\n?)+)", text, re.M)
        if perm:
            for line in perm.group(1).splitlines():
                m = re.match(r"^[ \t]+(\w+):\s*(\w+)?\s*$", line)
                if m:
                    tools[m.group(1).lower()] = (m.group(2) or "unknown").lower()
        result[agent] = {
            "model": model.group(1).strip() if model else "?",
            "steps": int(steps.group(1)) if steps else 0,
            "tools": tools,
        }
    return result


def build_tools_section(toolcalls, agents_perms=None, agents_dir=None):
    """คืน {catalog_n, matrix:[{tool,category,desc,allowed_by,denied_by,calls,errors,agents,last_used}], agents:{...}}."""
    if agents_perms is None:
        agents_perms = parse_agent_permissions(agents_dir)
    toolcalls = [r for r in (toolcalls or []) if isinstance(r, dict)]

    usage = {}
    for r in toolcalls:
        key = normalize_tool(r.get("tool"))
        u = usage.setdefault(key, {"calls": 0, "denied": 0, "errors": 0, "agents": {},
                                   "last_used": None, "raw_names": set()})
        if r.get("status") == "denied":
            u["denied"] += 1
            continue
        u["calls"] += 1
        if r.get("status") == "error":
            u["errors"] += 1
        ag = r.get("agent") or "?"
        u["agents"][ag] = u["agents"].get(ag, 0) + 1
        u["raw_names"].add(str(r.get("tool") or "?"))
        if r.get("time") and (u["last_used"] is None or str(r["time"]) > str(u["last_used"])):
            u["last_used"] = r["time"]

    names = list(CATALOG_ORDER) + sorted(n for n in usage if n not in CATALOG_ORDER)
    matrix = []
    for key in names:
        cat, desc = CATALOG_DESC.get(key, (categorize(key), "พบในล็อก (custom/MCP)"))
        allowed_by, denied_by = [], []
        for agent, info in sorted(agents_perms.items()):
            perm = (info.get("tools") or {}).get(key)
            if perm == "allow":
                allowed_by.append(agent)
            elif perm == "deny":
                denied_by.append(agent)
        u = usage.get(key, {"calls": 0, "denied": 0, "errors": 0,
                              "agents": {}, "last_used": None})
        if denied_by and u["denied"] > 0:
            state = "denied_attempted"
        elif allowed_by and u["calls"] > 0:
            state = "allowed_used"
        elif allowed_by:
            state = "allowed_unused"
        elif u["calls"] > 0 or u["denied"] > 0:
            state = "unlisted_used"
        else:
            state = "no_data"
        matrix.append({
            "tool": key, "category": cat, "desc": desc,
            "allowed_by": allowed_by, "denied_by": denied_by,
            "calls": u["calls"], "denied_attempts": u["denied"],
            "errors": u["errors"], "audit_state": state,
            "agents": u["agents"], "last_used": u["last_used"],
        })
    agents = {}
    for agent, info in sorted(agents_perms.items()):
        tools = info.get("tools") or {}
        agents[agent] = {
            "model": info.get("model", "?"), "steps": info.get("steps", 0),
            "allowed": sorted(t for t, v in tools.items() if v == "allow"),
            "denied": sorted(t for t, v in tools.items() if v == "deny"),
        }
    return {"catalog_n": len(CATALOG_ORDER), "matrix": matrix, "agents": agents}
