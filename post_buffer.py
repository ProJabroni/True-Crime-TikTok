#!/usr/bin/env python3
"""Send the finished video to TikTok through Buffer, and log it in state/history.json.

Needs the BUFFER_API_KEY secret. Optional: BUFFER_CHANNEL_ID (otherwise the first
TikTok channel in your Buffer account is used). VIDEO_URL is the public GitHub Pages
link to the video, set by the workflow.
"""
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text())
META = json.loads((ROOT / "out" / "meta.json").read_text())
HISTORY = ROOT / "state" / "history.json"
API = "https://api.buffer.com"


def log(*args):
    print(*args, flush=True)


def record(status, **extra):
    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else []
    history.append({"date": META["date"], "title": META["title"],
                    "wikipedia": META["wikipedia_url"], "seconds": META["seconds"],
                    "status": status, **extra})
    HISTORY.write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n")


def wait_until_online(url, size, minutes=15):
    """GitHub Pages takes a minute or two to publish; Buffer needs the link to work."""
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        try:
            r = requests.head(url, timeout=30, allow_redirects=True)
            if r.status_code == 200 and int(r.headers.get("content-length", size)) == size:
                log(f"Video is online: {url}")
                return
            log(f"  waiting for GitHub Pages ({r.status_code})")
        except requests.RequestException as e:
            log(f"  waiting for GitHub Pages ({e})")
        time.sleep(20)
    sys.exit(f"Video never appeared at {url}. Is GitHub Pages turned on (Settings > Pages > gh-pages)?")


def gql(key, query, variables=None):
    r = requests.post(API, headers={"Authorization": f"Bearer {key}"},
                      json={"query": query, "variables": variables or {}}, timeout=60)
    try:
        data = r.json()
    except ValueError:
        sys.exit(f"Buffer API error {r.status_code}: {r.text[:300]}")
    if r.status_code == 401:
        sys.exit("Buffer rejected the API key. Check the BUFFER_API_KEY secret.")
    return data


def tiktok_channel(key):
    data = gql(key, "query { account { organizations { id name } } }")
    if data.get("errors"):
        sys.exit(f"Buffer error: {data['errors']}")
    for org in data["data"]["account"]["organizations"]:
        q = 'query { channels(input: {organizationId: %s}) { id name service } }' % json.dumps(org["id"])
        chans = gql(key, q)
        for ch in (chans.get("data") or {}).get("channels") or []:
            if str(ch.get("service", "")).lower() == "tiktok":
                log(f"Using Buffer TikTok channel: {ch.get('name')}")
                return ch["id"]
    sys.exit("No TikTok channel found in Buffer. Connect TikTok in Buffer first.")


def main():
    key = os.environ.get("BUFFER_API_KEY", "").strip()
    url = os.environ.get("VIDEO_URL", "").strip()
    if not key:
        log("BUFFER_API_KEY not set, so the video was made but not posted.")
        record("made, not posted (no Buffer key)", video=url)
        return
    if not url:
        sys.exit("VIDEO_URL is missing.")

    wait_until_online(url, (ROOT / "out" / "video.mp4").stat().st_size)
    channel = os.environ.get("BUFFER_CHANNEL_ID", "").strip() or tiktok_channel(key)

    mutation = """mutation($input: CreatePostInput!) {
      createPost(input: $input) {
        __typename
        ... on PostActionSuccess { post { id } }
        ... on MutationError { message }
      }
    }"""
    post = {"channelId": channel, "text": META["caption"], "schedulingType": "automatic",
            "mode": CFG.get("buffer_mode", "addToQueue"), "assets": [{"video": {"url": url}}]}
    # Ask Buffer to switch on TikTok's AI-generated content label; retry without if unsupported.
    attempts = [{**post, "metadata": {"tiktok": {"isAiGenerated": True}}}, post]
    for i, data_input in enumerate(attempts):
        data = gql(key, mutation, {"input": data_input})
        if data.get("errors"):
            log(f"Buffer rejected the request: {data['errors']}")
            if i == 0:
                log("Retrying without the AI-label setting")
                continue
            record("Buffer error", video=url, error=str(data["errors"])[:500])
            sys.exit(1)
        result = data["data"]["createPost"]
        if result.get("__typename") == "PostActionSuccess":
            post_id = result["post"]["id"]
            log(f"Queued in Buffer for TikTok (post {post_id}).")
            if i == 1:
                log("Note: turn on the AI-generated label for these posts in TikTok/Buffer.")
            record("queued in Buffer", video=url, buffer_post=post_id, ai_label=(i == 0))
            return
        record("Buffer error", video=url, error=result.get("message", str(result)))
        sys.exit(f"Buffer could not create the post: {result.get('message', result)}")


if __name__ == "__main__":
    main()
