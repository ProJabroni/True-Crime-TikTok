#!/usr/bin/env python3
"""Print how many TikTok posts are already waiting (scheduled) in Buffer.

Used by the daily workflow to avoid piling up videos: if one is still waiting, today's
scheduled run skips. Prints 0 if anything goes wrong, so a Buffer hiccup never blocks a video.
"""
import os

import requests

API = "https://api.buffer.com"


def gql(key, query, variables=None):
    r = requests.post(API, headers={"Authorization": f"Bearer {key}"},
                      json={"query": query, "variables": variables or {}}, timeout=60)
    return r.json()


def waiting_tiktok_posts(key):
    orgs = gql(key, "query { account { organizations { id } } }")["data"]["account"]["organizations"]
    count = 0
    query = ("query($input: PostsInput!) { posts(input: $input, first: 50) "
             "{ edges { node { id status channelService } } } }")
    for org in orgs:
        data = gql(key, query, {"input": {"organizationId": org["id"], "filter": {"status": ["scheduled"]}}})
        if data.get("errors"):
            raise RuntimeError(str(data["errors"])[:300])
        for edge in data["data"]["posts"]["edges"]:
            node = edge["node"]
            if str(node.get("channelService", "")).lower() == "tiktok" and node.get("status") == "scheduled":
                count += 1
    return count


if __name__ == "__main__":
    key = os.environ.get("BUFFER_API_KEY", "").strip()
    try:
        print(waiting_tiktok_posts(key) if key else 0)
    except Exception as e:  # noqa: BLE001
        import sys
        print(f"could not read the Buffer queue: {e}", file=sys.stderr)
        print(0)
