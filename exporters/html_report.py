# Generates a standalone HTML investigation report
from pathlib import Path
from datetime import datetime
from core.target_model import Target


def generate(target: Target, cases_dir: Path = Path("cases")) -> Path:
    slug = target.case_slug
    folder = cases_dir / slug
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "report.html"

    verified_entities = [e for e in target.entities if e.metadata.get("verified") is True]
    uncertain_entities = [e for e in target.entities if e.metadata.get("verified") is not True]

    def render_entity_row(e, status_type):
        plat = f'<span class="tag g">{e.platform}</span>' if e.platform else ""
        if "holehe" in e.sources or e.metadata.get("status") == "possible registration (unverified)":
            plat += ' <span class="tag y">possible registration (unverified)</span>'
        reason = e.metadata.get("verification_reason", "")
        reason_tag = f' <span class="tag y">{reason}</span>' if (reason and status_type == "uncertain") else ""
        url = e.metadata.get("url", "")
        val_display = f'<a href="{url}" target="_blank" style="color:inherit;text-decoration:underline;">{e.value}</a>' if url else e.value
        v_icon = '<span class="tag g">✓</span>' if status_type == "verified" else '<span class="tag y">?</span>'
        return f"""
        <tr>
          <td>{e.entity_type} {v_icon}</td>
          <td>{val_display} {plat}{reason_tag}</td>
          <td>{e.sources[0] if e.sources else ''}</td>
          <td>{e.confidence:.0%}</td>
        </tr>"""

    entities_rows = ""
    if verified_entities:
        entities_rows += '<tr><td colspan="4" style="background:#eef6ee;font-weight:700;color:#2a7a3a;padding:6px 8px;font-size:11px;letter-spacing:1px;text-transform:uppercase;">Verified Accounts</td></tr>'
        for e in verified_entities:
            entities_rows += render_entity_row(e, "verified")

    if uncertain_entities:
        entities_rows += '<tr><td colspan="4" style="background:#fef3c7;font-weight:700;color:#92400e;padding:6px 8px;font-size:11px;letter-spacing:1px;text-transform:uppercase;">Uncertain Accounts</td></tr>'
        for e in uncertain_entities:
            entities_rows += render_entity_row(e, "uncertain")

    rejected_entities = getattr(target, "rejected_entities", [])
    rejected_html = ""
    if rejected_entities:
        rejected_rows = ""
        for r in rejected_entities:
            r_plat = r.platform or "Unknown"
            r_url = r.metadata.get("url", "")
            r_reason = r.metadata.get("verification_reason", "rejected")
            url_display = f'<a href="{r_url}" target="_blank" style="color:#8a2010;text-decoration:underline;">{r_url}</a>' if r_url else "N/A"
            rejected_rows += f"""
            <tr>
              <td><span class="tag r">{r_plat}</span></td>
              <td>{url_display}</td>
              <td><span class="tag y">{r_reason}</span></td>
            </tr>"""
        rejected_html = f"""
        <details style="margin-top:16px;">
          <summary style="cursor:pointer;font-weight:700;color:#8b2010;font-size:11px;letter-spacing:1px;text-transform:uppercase;">
            Rejected (probably false positives) ({len(rejected_entities)})
          </summary>
          <table style="margin-top:10px;">
            <tr><th>Platform</th><th>URL</th><th>Reason</th></tr>
            {rejected_rows}
          </table>
        </details>"""

    exposure_rows = ""
    for b in target.breaches:
        fields = ", ".join(b.exposed_fields) if b.exposed_fields else "N/A"
        date_str = b.date if b.date else "Unknown"
        src_tag = f'<span class="tag g">{b.source}</span>' if b.source else ""
        exposure_rows += f"""
        <tr>
          <td><span class="tag r">{b.name}</span></td>
          <td>{date_str}</td>
          <td>{fields}</td>
          <td>{src_tag}</td>
        </tr>"""

    provider_notes = []
    for note in target.notes:
        if any(p in note for p in ("XposedOrNot", "Hudson Rock", "BreachDirectory")):
            status_tag = "g"
            if "rate limited" in note or "skipped" in note:
                status_tag = "y"
            elif "error" in note or "failed" in note:
                status_tag = "r"
            provider_notes.append(f'<span class="tag {status_tag}">{note}</span>')
    provider_notes_html = " ".join(provider_notes)

    try:
        from core.orchestrator import get_sources_and_statuses
        sources_status = get_sources_and_statuses(target)
    except Exception:
        sources_status = {}

    sources_rows = ""
    tag_map = {
        "ok": "g",
        "skipped": "y",
        "rate limited": "y",
        "failed": "r",
    }
    for src_name, info in sources_status.items():
        st = info.get("status", "ok")
        dt = info.get("detail", "")
        badge = f'<span class="tag {tag_map.get(st, "y")}">{st}</span>'
        display_name = src_name.replace("_", " ").title()
        sources_rows += f"""
        <tr>
          <td><strong>{display_name}</strong></td>
          <td>{badge}</td>
          <td style="color:#7a5030;">{dt}</td>
        </tr>"""

    timeline_items = ""
    for item in target.timeline:
        ts = (item.get("at") or datetime.now().isoformat())[:19].replace("T", " ")
        timeline_items += f"""
        <div class="tl-item">
          <span class="tl-time">{ts}</span>
          <span class="tl-event">{item['event']}</span>
          <span class="tl-data">{item.get('data', {})}</span>
        </div>"""

    notes_html = ""
    for note in target.notes:
        notes_html += f'<div class="note">{note}</div>'

    risk_pct = int(target.risk_score * 100)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>J.A.R.V.I.S. — {target.primary}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    background:#f5f0e5;color:#3a2010;padding:40px;}}
  .header{{background:#ece6d8;border:1px solid #d8cdb8;border-radius:12px;
    padding:28px 32px;margin-bottom:24px;display:flex;
    justify-content:space-between;align-items:flex-start}}
  .header-left h1{{font-size:22px;font-weight:700;color:#2c1810}}
  .header-left .sub{{font-size:12px;color:#a08060;margin-top:4px;
    letter-spacing:1px;text-transform:uppercase}}
  .risk-badge{{background:#fdf0e8;border:1px solid #e8b090;
    border-radius:8px;padding:12px 20px;text-align:center}}
  .risk-num{{font-size:28px;font-weight:800;color:#8b2010}}
  .risk-label{{font-size:10px;color:#a08060;text-transform:uppercase;
    letter-spacing:1px}}
  .card{{background:#fdfaf4;border:1px solid #e0d4c0;border-radius:10px;
    padding:24px;margin-bottom:20px}}
  .card h2{{font-size:13px;font-weight:800;letter-spacing:2px;
    text-transform:uppercase;color:#b09070;margin-bottom:16px}}
  table{{width:100%;border-collapse:collapse;font-size:12px}}
  th{{text-align:left;font-size:9px;font-weight:800;letter-spacing:2px;
    text-transform:uppercase;color:#b09070;padding:0 0 8px;
    border-bottom:1px solid #e8dfc8}}
  td{{padding:8px 0;border-bottom:1px solid #f5f0e5;color:#5a3820;
    vertical-align:top}}
  .tag{{display:inline-block;font-size:9px;font-weight:700;
    padding:2px 7px;border-radius:10px;margin-left:4px}}
  .tag.r{{background:#fdf0e8;color:#9a2a0a;border:1px solid #e8b090}}
  .tag.g{{background:#eef6ee;color:#2a7a3a;border:1px solid #b8dfc0}}
  .tag.y{{background:#fef3c7;color:#92400e;border:1px solid #fcd34d}}
  .rbar-wrap{{display:flex;align-items:center;gap:10px;margin-top:8px}}
  .rbar{{height:6px;width:200px;background:#e8dfc8;border-radius:3px}}
  .rfill{{height:100%;background:linear-gradient(90deg,#c8760a,#c0392b);
    border-radius:3px;width:{risk_pct}%}}
  .tl-item{{display:flex;gap:16px;padding:8px 0;
    border-bottom:1px solid #f5f0e5;font-size:11px}}
  .tl-time{{color:#b09070;min-width:140px;font-family:monospace}}
  .tl-event{{color:#5a2810;font-weight:600;min-width:140px}}
  .tl-data{{color:#a09070}}
  .note{{background:#fdf6ec;border-left:3px solid #c8945a;
    padding:8px 12px;border-radius:0 6px 6px 0;
    margin-bottom:8px;font-size:12px;color:#7a5030;font-style:italic}}
  .footer{{text-align:center;font-size:10px;color:#b09878;
    margin-top:32px;padding-top:16px;border-top:1px solid #e0d4c0}}
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <h1>{target.primary}</h1>
    <div class="sub">{target.target_type} ·
      opened {target.opened_at[:10]} ·
      {len(target.entities)} entities ·
      {len(target.breaches)} breaches</div>
  </div>
  <div class="risk-badge">
    <div class="risk-num">{target.risk_score:.2f}</div>
    <div class="risk-label">risk score</div>
    <div class="rbar-wrap">
      <div class="rbar"><div class="rfill"></div></div>
    </div>
  </div>
</div>

<div class="card">
  <h2>Sources and status</h2>
  <table>
    <tr><th>Source</th><th>Status</th><th>Details</th></tr>
    {sources_rows or '<tr><td colspan="3">No sources recorded</td></tr>'}
  </table>
</div>

<div class="card">
  <h2>Entities</h2>
  <table>
    <tr><th>Type</th><th>Value</th><th>Source</th><th>Confidence</th></tr>
    {entities_rows or '<tr><td colspan="4">No entities found</td></tr>'}
  </table>
  {rejected_html}
</div>

<div class="card">
  <h2>Email exposure</h2>
  {f'<div style="margin-bottom:12px">{provider_notes_html}</div>' if provider_notes_html else ''}
  <table>
    <tr><th>Name</th><th>Date</th><th>Data Types</th><th>Source Provider</th></tr>
    {exposure_rows or '<tr><td colspan="4">No breaches found in queried sources</td></tr>'}
  </table>
</div>

{f'<div class="card"><h2>Notes</h2>{notes_html}</div>'
  if target.notes else ''}

<div class="card">
  <h2>Timeline</h2>
  {timeline_items or '<div style="color:#b09070;font-size:12px">No events</div>'}
</div>

<div class="footer">
  Generated by J.A.R.V.I.S. · {generated}
</div>

</body>
</html>"""

    path.write_text(html)
    return path