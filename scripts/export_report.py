#!/usr/bin/env python3
"""
export_report.py — Scopeboard Markdown/PDF export (stdlib only)

Reads dashboard/data.json (or all-projects.json with --all) and writes
reports/scopeboard-YYYY-MM-DD.md (19 sections) + optional PDF via HTML print fallback.

Usage:
  python scripts/export_report.py --format md --out reports/report.md
  python scripts/export_report.py --format pdf --out reports/report.pdf
  python scripts/export_report.py --format both --out reports/report
  python scripts/export_report.py --all --format md --out reports/combined.md
  python scripts/export_report.py --project Agent --format md

PDF is optional: window.print() is primary (HTML). This script creates a printable
HTML then tries weasyprint/reportlab if available, otherwise keeps HTML.

Design: TASK-EXPORT-01 (reuse data.json as source of truth, no KPI recalculation)
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MANAGER_DIR = os.path.join(ROOT, ".agent", "manager")
DEFAULT_DATA = os.path.join(ROOT, "dashboard", "data.json")
DEFAULT_ALL = os.path.join(ROOT, "dashboard", "all-projects.json")
DEFAULT_OUT_DIR = os.path.join(ROOT, "reports")
EVENTS_FILE = os.path.join(MANAGER_DIR, "events.jsonl")


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _h2(title):
    return f"## {title}\n\n"

def _table(headers, rows):
    if not rows:
        return "| " + " | ".join(headers) + " |\n| " + " | ".join(["---"]*len(headers)) + " |\n| _no data_ | \n\n"
    out = "| " + " | ".join(headers) + " |\n"
    out += "| " + " | ".join(["---"]*len(headers)) + " |\n"
    for r in rows:
        out += "| " + " | ".join(str(x) if x is not None else "" for x in r) + " |\n"
    out += "\n"
    return out


def build_markdown(payload, mode="single"):
    """Build Markdown report with ~19 sections from dashboard/data.json payload."""
    is_all = mode == "all"
    kpis = (payload.get("combined") if is_all else payload.get("kpis")) or payload.get("kpis") or {}
    per_task = payload.get("per_task") or payload.get("per_project") or []
    # if all mode, per_project is projects
    combined = payload.get("combined") if is_all else None
    generated = payload.get("generated_at", datetime.now(timezone.utc).isoformat())
    title = "Scopeboard Report — Combined Projects" if is_all else "Scopeboard Report — Single Project"
    project_name = payload.get("project", "Agent") if not is_all else f"{len(payload.get('per_project', []))} projects"

    md = f"# {title}\n\n"
    md += f"> Generated: {generated} | Project: {project_name} | Mode: {mode}\n\n"
    md += "> Scopeboard — Observability Dashboard (file-based, stdlib only) — 19 sections derived from `dashboard/data.json`\n\n"

    # 1 KPIs
    md += _h2("1. KPIs")
    md += _table(["Metric", "Value"], [
        ["Total tasks", kpis.get("total_tasks","")],
        ["Done", kpis.get("done","")],
        ["Failed", kpis.get("failed","")],
        ["Cancelled", kpis.get("cancelled","")],
        ["Success rate", kpis.get("success_rate","")],
        ["Total tool calls", kpis.get("total_tool_calls","")],
        ["Recoveries", kpis.get("recoveries","")],
        ["Review fails", kpis.get("review_fails","")],
        ["Open alerts", kpis.get("open_alerts","")],
        ["Tokens total", kpis.get("tokens_total","")],
    ])

    # 2 Per-task / Per-project
    md += _h2("2. Per-Task Summary" if not is_all else "2. Per-Project Summary")
    if not is_all:
        rows = []
        for t in (per_task or [])[:30]:
            rows.append([t.get("task",""), "✓" if t.get("done") else "✗", t.get("sessions",""), t.get("tool_calls",""), t.get("recovery",""), t.get("review_loop",""), t.get("review_passed","")])
        md += _table(["Task", "Done", "Sessions", "Tool calls", "Recovery", "Review loop", "Passed"], rows)
    else:
        rows = []
        for p in (payload.get("per_project") or [])[:30]:
            rows.append([p.get("project",""), p.get("done",""), p.get("total_tool_calls",""), p.get("success_rate","")])
        md += _table(["Project", "Done", "Tool calls", "Success"], rows)

    # 3 Efficiency
    eff = payload.get("efficiency") or {}
    if eff:
        md += _h2("3. Efficiency")
        rows = []
        for k, v in list(eff.items())[:10]:
            rows.append([k, json.dumps(v)[:80] if isinstance(v, (dict, list)) else v])
        if rows:
            md += _table(["Key", "Value"], rows)

    # 4 Execution graph
    graph = payload.get("execution") or payload.get("graph") or {}
    if graph:
        md += _h2("4. Execution Graph")
        md += "```text\n" + json.dumps(graph, indent=2, ensure_ascii=False)[:2000] + "\n```\n\n"

    # 5 Queue
    queue = payload.get("queue") or {}
    if queue:
        md += _h2("5. Queue")
        tasks = queue.get("tasks") or []
        rows = [[t.get("id",""), t.get("status",""), t.get("type",""), ",".join(t.get("dependencies") or [])] for t in tasks[:20]]
        md += _table(["ID", "Status", "Type", "Deps"], rows)

    # 6 Alerts / Risk
    alerts = payload.get("alerts") or payload.get("risk") or {}
    if alerts:
        md += _h2("6. Risk / Alerts")
        md += "```json\n" + json.dumps(alerts, indent=2, ensure_ascii=False)[:2000] + "\n```\n\n"

    # 7 Failure taxonomy
    failure = payload.get("failure") or {}
    if failure:
        md += _h2("7. Failure Taxonomy")
        rows = []
        if isinstance(failure, dict):
            for k, v in list(failure.items())[:10]:
                rows.append([str(k), json.dumps(v)[:80] if isinstance(v, (dict, list)) else str(v)[:80]])
        elif isinstance(failure, list):
            for item in failure[:10]:
                if isinstance(item, dict):
                    rows.append([item.get("task",""), item.get("category",""), str(item.get("count",""))[:40]])
                else:
                    rows.append([str(item)[:40], ""])
        if rows:
            md += _table(["Category", "Count"], rows)

    # 8 Contract
    contract = payload.get("contract") or {}
    if contract:
        md += _h2("8. Contract")
        md += "```json\n" + json.dumps(contract, indent=2, ensure_ascii=False)[:2000] + "\n```\n\n"

    # 9 Tools
    tools = payload.get("tools") or {}
    if tools:
        md += _h2("9. Tools Inventory")
        rows = []
        for k, v in list(tools.items())[:15]:
            rows.append([k, str(v)[:50]])
        if rows:
            md += _table(["Tool", "Usage"], rows)

    # 10 History
    history = payload.get("history") or {}
    if history:
        md += _h2("10. History")
        rows = []
        for d, vals in list(history.items())[:10]:
            rows.append([d, json.dumps(vals)[:60]])
        if rows:
            md += _table(["Date", "Snapshot"], rows)

    # 11 Timeline
    timeline = payload.get("timeline") or payload.get("recent_activity") or []
    if timeline:
        md += _h2("11. Timeline / Recent Activity")
        rows = []
        for e in timeline[:20]:
            if isinstance(e, dict):
                t = str(e.get("time") or "")[:19]
                rows.append([t, e.get("event",""), e.get("task","")])
            else:
                rows.append([str(e)[:19], "", ""])
        md += _table(["Time", "Event", "Task"], rows)

    # 12 Errors
    errors = payload.get("errors") or []
    if errors and isinstance(errors, list):
        md += _h2("12. Errors")
        rows = [[str(e.get("time") or "")[:19], e.get("task",""), str(e.get("error") or "")[:60]] for e in errors[:15] if isinstance(e, dict)]
        md += _table(["Time", "Task", "Error"], rows)
    elif errors and isinstance(errors, dict):
        md += _h2("12. Errors")
        rows = [[str(k)[:19], str(v)[:60]] for k, v in list(errors.items())[:15]]
        md += _table(["Key", "Value"], rows)

    # 13 Findings
    findings = payload.get("findings") or []
    if findings and isinstance(findings, list):
        md += _h2("13. Findings")
        rows = [[str(f.get("severity") or ""), f.get("file",""), str(f.get("issue") or "")[:60]] for f in findings[:15] if isinstance(f, dict)]
        md += _table(["Severity", "File", "Issue"], rows)
    elif findings and isinstance(findings, dict):
        md += _h2("13. Findings")
        rows = [[str(k)[:20], str(v)[:60]] for k, v in list(findings.items())[:15]]
        md += _table(["Key", "Value"], rows)

    # 14 Durations
    durations = payload.get("durations") or {}
    if durations:
        md += _h2("14. Durations")
        rows = []
        for k, v in list(durations.items())[:10]:
            rows.append([k, str(v)[:40]])
        if rows:
            md += _table(["Metric", "Value"], rows)

    # 15 Agents
    agents = payload.get("agents") or {}
    if agents:
        md += _h2("15. Agents")
        rows = []
        if isinstance(agents, dict):
            for k, v in list(agents.items())[:10]:
                rows.append([str(k), json.dumps(v)[:60] if isinstance(v, (dict, list)) else str(v)[:60]])
        elif isinstance(agents, list):
            for a in agents[:10]:
                if isinstance(a, dict):
                    rows.append([a.get("agent",""), json.dumps(a)[:60]])
                else:
                    rows.append([str(a)[:40], ""])
        if rows:
            md += _table(["Agent", "Stats"], rows)

    # 16 Trend
    trend = payload.get("trend") or {}
    if trend:
        md += _h2("16. Trend")
        md += "```json\n" + json.dumps(trend, indent=2, ensure_ascii=False)[:2000] + "\n```\n\n"

    # 17 Files changed
    files = payload.get("files_changed") or []
    if files:
        md += _h2("17. Files Changed")
        rows = [[f] for f in files[:20]]
        md += _table(["File"], rows)

    # 18 Reviews
    reviews = payload.get("reviews") or {}
    if reviews:
        md += _h2("18. Reviews")
        rows = []
        for rid, r in list(reviews.items())[:10] if isinstance(reviews, dict) else []:
            rows.append([rid, r.get("status","") if isinstance(r, dict) else str(r)[:40]])
        if rows:
            md += _table(["Review", "Status"], rows)

    # 19 Audit
    audit = payload.get("audit") or {}
    if audit:
        md += _h2("19. Audit")
        md += "```json\n" + json.dumps(audit, indent=2, ensure_ascii=False)[:2000] + "\n```\n\n"

    # Always add footer
    md += "---\n*Generated by `scripts/export_report.py` from `dashboard/data.json` (19 sections, stdlib only)*\n"
    return md


def export_markdown(payload, out_path, mode="single"):
    md = build_markdown(payload, mode)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"[EXPORT] Markdown {len(md)} chars -> {out_path}")
    # log event
    try:
        with open(EVENTS_FILE, "a", encoding="utf-8") as ef:
            ef.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "event": "EXPORT_MD", "task": f"EXPORT-{os.path.basename(out_path)}", "mode": mode}, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return out_path


def export_pdf(payload, out_path, mode="single"):
    """Create printable HTML then try to convert to PDF if weasyprint/reportlab available."""
    html = build_html(payload, mode)
    html_path = out_path.replace(".pdf", ".html") if out_path.endswith(".pdf") else out_path + ".html"
    os.makedirs(os.path.dirname(os.path.abspath(html_path)), exist_ok=True)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[EXPORT] Printable HTML -> {html_path} (open in browser and Print to PDF)")
    # try weasyprint
    pdf_path = out_path if out_path.endswith(".pdf") else out_path + ".pdf"
    try:
        import weasyprint  # type: ignore
        weasyprint.HTML(filename=html_path).write_pdf(pdf_path)
        print(f"[EXPORT] PDF via weasyprint -> {pdf_path}")
        return pdf_path
    except Exception as e:
        print(f"[EXPORT] weasyprint not available ({e}) — keep HTML for window.print()")
        pass
    try:
        from reportlab.pdfgen import canvas  # type: ignore
        from reportlab.lib.pagesizes import A4
        c = canvas.Canvas(pdf_path, pagesize=A4)
        c.drawString(30, 800, "Scopeboard Report — see HTML for full content")
        c.drawString(30, 780, f"Generated: {datetime.now(timezone.utc).isoformat()}")
        c.save()
        print(f"[EXPORT] PDF via reportlab (stub) -> {pdf_path}")
        return pdf_path
    except Exception as e:
        print(f"[EXPORT] reportlab not available ({e}) — use HTML print")
        return html_path
    return html_path


def build_html(payload, mode="single"):
    md = build_markdown(payload, mode)
    # very simple markdown to HTML (headers + tables remain as text, but printable)
    html = f"""<!doctype html><meta charset=\"utf-8\"><title>Scopeboard Report</title>
<style>
body{{font-family:Segoe UI, sans-serif; margin:40px; color:#111}}
h1{{border-bottom:2px solid #222; padding-bottom:8px}}
h2{{color:#0b57d0; margin-top:32px}}
table{{border-collapse:collapse; width:100%; margin:12px 0}}
th,td{{border:1px solid #ccc; padding:6px 8px; text-align:left; font-size:13px}}
pre{{background:#f6f8fa; padding:12px; overflow:auto}}
@media print{{body{{margin:12mm}} button{{display:none}}}}
</style>
<button onclick=\"window.print()\" style=\"position:fixed;right:20px;top:20px;padding:8px 14px\">Print / Save PDF</button>
<pre style=\"white-space:pre-wrap; font-family:ui-monospace, monospace\">{md[:8000].replace('<','&lt;')}</pre>
<pre>Full Markdown report — open .md file for complete 19 sections</pre>
"""
    return html


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scopeboard Markdown/PDF export (stdlib only)")
    ap.add_argument("--format", choices=["md", "pdf", "both", "html"], default="md", help="output format")
    ap.add_argument("--out", dest="out", default=None, help="output path (dir or file)")
    ap.add_argument("--all", action="store_true", help="use all-projects.json (combined) instead of data.json")
    ap.add_argument("--project", default=None, help="filter for single project when using --all (not yet)")
    ap.add_argument("--date", default=None, help="date string for filename (default today)")
    args = ap.parse_args(argv)

    data_path = DEFAULT_ALL if args.all else DEFAULT_DATA
    payload = load_json(data_path, {}) or {}
    if not payload:
        print(f"[EXPORT] No payload at {data_path} — run export_json.py first", file=sys.stderr)
        payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "kpis": {}, "per_task": []}

    mode = "all" if args.all else "single"
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    if args.out:
        out_base = args.out
        # if out is dir
        if os.path.isdir(out_base) or out_base.endswith("/") or out_base.endswith("\\"):
            out_base = os.path.join(out_base, f"scopeboard-{date_str}")
    else:
        os.makedirs(DEFAULT_OUT_DIR, exist_ok=True)
        out_base = os.path.join(DEFAULT_OUT_DIR, f"scopeboard-{date_str}")

    fmt = args.format
    outs = []
    if fmt in ("md", "both"):
        md_path = out_base if out_base.endswith(".md") else out_base + ".md"
        outs.append(export_markdown(payload, md_path, mode))
    if fmt in ("pdf", "both", "html"):
        pdf_path = out_base if out_base.endswith(".pdf") else out_base + ".pdf"
        # html is primary
        outs.append(export_pdf(payload, pdf_path, mode))
    if fmt == "html":
        html_path = out_base if out_base.endswith(".html") else out_base + ".html"
        html = build_html(payload, mode)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[EXPORT] HTML -> {html_path}")
        outs.append(html_path)

    for o in outs:
        print(o)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
