#!/usr/bin/env python3
"""Report what Buffer knows about the videos we sent it (writes state/buffer-status.txt).

Started by editing check-buffer.txt. Needs the BUFFER_API_KEY secret.
"""
import json
import os
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
API = "https://api.buffer.com"
KEY = os.environ.get("BUFFER_API_KEY", "").strip()
lines = []


def out(*args):
    text = " ".join(str(a) for a in args)
    print(text, flush=True)
    lines.append(text)


def gql(query, variables=None):
    r = requests.post(API, headers={"Authorization": f"Bearer {KEY}"},
                      json={"query": query, "variables": variables or {}}, timeout=60)
    try:
        return r.json()
    except ValueError:
        return {"errors": [f"HTTP {r.status_code}: {r.text[:300]}"]}


def type_info(name):
    d = gql('query($n: String!) { __type(name: $n) { kind enumValues { name } '
            'fields { name type { kind name ofType { kind name } } } '
            'inputFields { name type { kind name ofType { kind name } } } } }', {"n": name})
    return (d.get("data") or {}).get("__type") or {}


def scalar_fields(t):
    names = []
    for f in t.get("fields") or []:
        ty = f["type"]
        kind = ty["kind"] if ty["kind"] != "NON_NULL" else ty["ofType"]["kind"]
        if kind in ("SCALAR", "ENUM"):
            names.append(f["name"])
    return names


def main():
    if not KEY:
        out("BUFFER_API_KEY secret is missing.")
        return
    history = json.loads((ROOT / "state" / "history.json").read_text())
    post_ids = [h["buffer_post"] for h in history if h.get("buffer_post")]

    orgs = gql("query { account { organizations { id name } } }")
    if orgs.get("errors"):
        out("Buffer error:", orgs["errors"])
        return
    for org in orgs["data"]["account"]["organizations"]:
        out(f"Organization: {org['name']}")
        q = 'query { channels(input: {organizationId: %s}) { id name service isQueuePaused } }' % json.dumps(org["id"])
        for ch in (gql(q).get("data") or {}).get("channels") or []:
            out(f"  Channel: {ch.get('name')} ({ch.get('service')}), queue paused: {ch.get('isQueuePaused')}")

    out("Post statuses Buffer uses:", [v["name"] for v in type_info("PostStatus").get("enumValues") or []])
    err_fields = scalar_fields(type_info("PostPublishingError"))
    post_input = [f["name"] for f in type_info("PostInput").get("inputFields") or []]
    out("PostInput fields:", post_input)

    fields = "id status dueAt sentAt createdAt shareMode schedulingType channelService text"
    if err_fields:
        fields += " error { %s }" % " ".join(err_fields)
    for pid in post_ids:
        d = gql("query($input: PostInput!) { post(input: $input) { %s } }" % fields, {"input": {"id": pid}})
        if d.get("errors"):
            out(f"Post {pid}: error {d['errors']}")
            continue
        p = d["data"]["post"]
        p["text"] = (p.get("text") or "")[:60]
        out(f"Post {pid}:", json.dumps(p, ensure_ascii=False))

    (ROOT / "state" / "buffer-status.txt").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
