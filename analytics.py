#!/usr/bin/env python3
"""Pull performance numbers for every video Buffer has posted, and write a readable report.

Writes state/analytics.json (raw numbers) and state/analytics.md (a table, newest first).
Started by editing check-buffer.txt (together with the Buffer status check). Needs BUFFER_API_KEY.
Buffer refreshes metrics about once a day, so very new posts can show nothing yet.
"""
import json
import os
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
API = "https://api.buffer.com"
KEY = os.environ.get("BUFFER_API_KEY", "").strip()


def gql(query, variables=None):
    r = requests.post(API, headers={"Authorization": f"Bearer {KEY}"},
                      json={"query": query, "variables": variables or {}}, timeout=60)
    return r.json()


def main():
    if not KEY:
        print("BUFFER_API_KEY secret is missing.")
        return
    history = json.loads((ROOT / "state" / "history.json").read_text())
    rows = []
    query = ("query($input: PostInput!) { post(input: $input) { id status sentAt dueAt "
             "metricsUpdatedAt metrics { type name value unit } } }")
    for h in history:
        pid = h.get("buffer_post")
        if not pid or "draft" in h.get("status", ""):
            continue
        d = gql(query, {"input": {"id": pid}})
        if d.get("errors"):
            rows.append({**h, "buffer_status": "not found (deleted in Buffer?)", "metrics": {}})
            continue
        p = d["data"]["post"]
        metrics = {m["type"]: m["value"] for m in (p.get("metrics") or [])}
        rows.append({"date": h["date"], "title": h["title"], "seconds": h.get("seconds"),
                     "wikipedia": h.get("wikipedia"), "buffer_post": pid, "buffer_status": p["status"],
                     "sent_at": p.get("sentAt"), "metrics_updated": p.get("metricsUpdatedAt"),
                     "metrics": metrics})
    (ROOT / "state" / "analytics.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")

    cols = ["views", "reactions", "comments", "shares", "reach", "engagementRate"]
    present = [c for c in cols if any(c in r["metrics"] for r in rows)]
    other = sorted({k for r in rows for k in r["metrics"]} - set(cols))
    lines = [f"# TikTok video performance (from Buffer, updated {datetime.utcnow():%Y-%m-%d %H:%M} UTC)", "",
             "| Posted | Title | Length | Status | " + " | ".join(present + other) + " |",
             "|---|---|---|---|" + "---|" * len(present + other)]
    for r in sorted(rows, key=lambda r: r.get("sent_at") or r["date"], reverse=True):
        when = (r.get("sent_at") or r["date"])[:10]
        vals = [str(r["metrics"].get(c, "")) for c in present + other]
        lines.append(f"| {when} | {r['title']} | {r.get('seconds') or ''}s | {r['buffer_status']} | "
                     + " | ".join(vals) + " |")
    if not present and not other:
        lines += ["", "Buffer has not returned any numbers yet (metrics refresh about once a day, "
                  "and may not be available for TikTok on every plan)."]
    (ROOT / "state" / "analytics.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
