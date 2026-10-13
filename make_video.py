#!/usr/bin/env python3
"""Make one narrated true-crime TikTok video from a Wikipedia article.

Pipeline: Wikipedia -> script (Gemini, or Cloudflare as fallback) -> images (Cloudflare
Workers AI) -> narration (Microsoft edge-tts) -> captions + Ken Burns video (ffmpeg).

Writes out/video.mp4 and out/meta.json, and records the article in state/used.json.
Settings live in config.json.
"""
import asyncio
import base64
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text())
STATE = ROOT / "state"
USED_FILE = STATE / "used.json"
OUT = ROOT / "out"
WORK = OUT / "work"
FPS = 30
W, H = 1080, 1920

REPO = os.environ.get("GITHUB_REPOSITORY", "local/true-crime-tiktok")
UA = f"TrueCrimeTikTok/1.0 (https://github.com/{REPO}; automated video bot)"
HTTP = requests.Session()
HTTP.headers["User-Agent"] = UA

CF_ACCOUNT = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
CF_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

SKIP_TITLES = re.compile(r"^(List|Lists|Timeline|Index|Outline) of", re.I)
END_SECTIONS = re.compile(
    r"\n==+\s*(See also|References|Notes|External links|Further reading|Bibliography|"
    r"Sources|Citations|Footnotes|In popular culture|Media|Popular culture)\s*==+",
    re.I,
)


def log(*args):
    print(*args, flush=True)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- Wikipedia

def wiki(params):
    params = {"format": "json", "formatversion": "2", **params}
    for attempt in range(5):
        r = HTTP.get("https://en.wikipedia.org/w/api.php", params=params, timeout=30)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(5 * (attempt + 1))
            continue
        if not r.ok:
            log(f"  Wikipedia answered {r.status_code}: {r.text[:300]}")
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def category_pages(cat, depth=2, budget=None):
    """Article titles in a category, including subcategories up to `depth` levels."""
    budget = budget if budget is not None else {"calls": 60}
    pages, subcats, cont = [], [], {}
    while budget["calls"] > 0:
        budget["calls"] -= 1
        data = wiki({"action": "query", "list": "categorymembers", "cmtitle": cat,
                     "cmtype": "page|subcat", "cmlimit": "500", **cont})
        for m in data.get("query", {}).get("categorymembers", []):
            if m["ns"] == 0:
                pages.append(m["title"])
            elif m["ns"] == 14:
                subcats.append(m["title"])
        if "continue" in data and len(pages) < 3000:
            cont = data["continue"]
        else:
            break
    if depth > 0:
        random.shuffle(subcats)
        for sc in subcats:
            if budget["calls"] <= 0:
                break
            pages += category_pages(sc, depth - 1, budget)
    return pages


def page_lengths(titles):
    lengths = {}
    for i in range(0, len(titles), 50):
        data = wiki({"action": "query", "prop": "info", "titles": "|".join(titles[i:i + 50])})
        for p in data.get("query", {}).get("pages", []):
            if not p.get("missing"):
                lengths[p["title"]] = p.get("length", 0)
    return lengths


def article(title):
    data = wiki({"action": "query", "prop": "extracts", "explaintext": "1",
                 "redirects": "1", "titles": title})
    page = data["query"]["pages"][0]
    text = END_SECTIONS.split(page.get("extract", ""))[0]
    text = re.sub(r"\n{2,}", "\n", text).strip()
    real_title = page.get("title", title)
    url = "https://en.wikipedia.org/wiki/" + requests.utils.quote(real_title.replace(" ", "_"))
    return real_title, text, url


def candidates(used):
    """A shortlist of unused, substantial articles from one random category."""
    cats = list(CFG["categories"])
    random.shuffle(cats)
    for cat in cats:
        log(f"Looking in {cat}")
        try:
            pages = category_pages(cat)
        except Exception as e:  # noqa: BLE001
            log(f"  could not read category: {e}")
            continue
        pages = [t for t in dict.fromkeys(pages) if t not in used and not SKIP_TITLES.match(t)]
        log(f"  {len(pages)} unused articles")
        if not pages:
            continue
        lengths = page_lengths(random.sample(pages, min(40, len(pages))))
        # Longer articles = better documented cases = richer stories.
        top = sorted(lengths, key=lengths.get, reverse=True)[:8]
        random.shuffle(top)
        if top:
            return top
    sys.exit("No unused articles found in any category. Add categories to config.json.")


# ---------------------------------------------------------------- script

SYSTEM_PROMPT = """You write narration scripts for vertical true-crime videos on TikTok, based ONLY on the Wikipedia article provided.

Rules:
- Stick strictly to facts in the article. Never invent details, dialogue, thoughts or motives.
- Anyone not convicted is a "suspect" or "accused"; keep the article's "allegedly" wording. Never name a living person as guilty unless the article says they were convicted.
- Be respectful toward victims and their families. No gore, no graphic descriptions of violence or injuries, no mockery, no jokes.
- If the article is not about a specific criminal case or criminal, is too thin to tell a story, or centers on sexual abuse of a child, return {"skip": true, "reason": "..."} and nothing else.

Otherwise return JSON exactly like:
{
  "skip": false,
  "title": "short on-screen title, max 6 words",
  "scenes": [ { "narration": "...", "image_prompt": "..." } ],
  "caption": "...",
  "hashtags": ["..."]
}
- {scenes_min} to {scenes_max} scenes. Scene 1 is the hook: one or two gripping sentences (a question or the most striking fact) that make people keep watching. Then tell the story in order: who, where, when, what happened, the investigation, twists. The last scene says how the case ended or where it stands today, then "Follow for more cases."
- Total narration {words_min} to {words_max} words. Short spoken sentences. Write numbers, dates and years the way they are read aloud. No emojis, no stage directions.
- image_prompt: describe ONE moody still image for that moment in plain visual terms: places, buildings, rooms, objects, vehicles, weather, time of day, era-appropriate details, silhouettes or figures seen from behind or far away. Never describe a real person's face, never violence, blood, bodies, injuries or weapons being used, never text, signs, newspapers or logos.
- caption: under 150 characters, intriguing, no spoilers. hashtags: 4 to 6, lowercase, without the # sign."""


def call_gemini(system, user):
    if not GEMINI_KEY:
        return None
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8},
    }
    for model in CFG["gemini_models"]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=240)
            if r.status_code in (429, 500, 502, 503, 504):
                log(f"  Gemini {model} busy ({r.status_code}), retrying")
                time.sleep(20 * (attempt + 1))
                continue
            break
        if r.status_code != 200:
            log(f"  Gemini {model} failed: {r.status_code} {r.text[:300]}")
            continue
        try:
            parts = r.json()["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            if text.strip():
                log(f"  script written by {model}")
                return text
        except (KeyError, IndexError):
            log(f"  Gemini {model} returned no text: {r.text[:300]}")
    return None


def call_cloudflare_text(system, user):
    if not (CF_ACCOUNT and CF_TOKEN):
        return None
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/v1/chat/completions"
    body = {"model": CFG["cloudflare_text_model"], "max_tokens": 3000, "temperature": 0.8,
            "messages": [{"role": "system", "content": system + "\nReply with the JSON only."},
                         {"role": "user", "content": user}]}
    for attempt in range(3):
        r = requests.post(url, headers={"Authorization": f"Bearer {CF_TOKEN}"}, json=body, timeout=240)
        if r.ok:
            log(f"  script written by {CFG['cloudflare_text_model']}")
            return r.json()["choices"][0]["message"]["content"]
        log(f"  Cloudflare text failed: {r.status_code} {r.text[:300]}")
        time.sleep(10 * (attempt + 1))
    return None


def parse_json(text):
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in model reply")
    return json.loads(text[start:end + 1])


def write_script(title, text):
    system = SYSTEM_PROMPT
    for key in ("scenes_min", "scenes_max", "words_min", "words_max"):
        system = system.replace("{" + key + "}", str(CFG[key]))
    user = f"Article title: {title}\n\n{text[:20000]}"
    raw = call_gemini(system, user) or call_cloudflare_text(system, user)
    if raw is None:
        sys.exit("No script model worked. Check the GEMINI_API_KEY / CLOUDFLARE_* secrets.")
    script = parse_json(raw)
    if script.get("skip"):
        return script
    scenes = [s for s in script.get("scenes", [])
              if isinstance(s, dict) and s.get("narration", "").strip() and s.get("image_prompt", "").strip()]
    if len(scenes) < 5:
        raise ValueError(f"script has only {len(scenes)} usable scenes")
    script["scenes"] = scenes[: CFG["scenes_max"] + 2]
    words = sum(len(s["narration"].split()) for s in script["scenes"])
    log(f"  {len(script['scenes'])} scenes, {words} words")
    return script


# ---------------------------------------------------------------- media

SAFE_PROMPT = "an empty rain-soaked street at night under a flickering streetlight, distant police lights"


def make_image(prompt, path, seed):
    if not (CF_ACCOUNT and CF_TOKEN):
        sys.exit("CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN secrets are required for images.")
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/{CFG['cloudflare_image_model']}"
    for attempt in range(5):
        base = prompt if attempt < 3 else SAFE_PROMPT  # last tries: something that can't be refused
        full = f"{base}, {CFG['image_style']}"[:2000]
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {CF_TOKEN}"},
                              json={"prompt": full, "steps": CFG["image_steps"], "seed": seed + attempt},
                              timeout=180)
            image = r.json().get("result", {}).get("image") if r.ok else None
            if image:
                path.write_bytes(base64.b64decode(image))
                return
            log(f"  image attempt {attempt + 1} failed: {r.status_code} {r.text[:200]}")
        except requests.RequestException as e:
            log(f"  image attempt {attempt + 1} failed: {e}")
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"could not generate image for: {prompt}")


async def make_voice(text, path):
    import edge_tts
    for attempt in range(5):
        try:
            await edge_tts.Communicate(text, CFG["voice"], rate=CFG["voice_rate"]).save(str(path))
            if path.exists() and path.stat().st_size > 2000:
                return
        except Exception as e:  # noqa: BLE001
            log(f"  voice attempt {attempt + 1} failed: {e}")
        await asyncio.sleep(5 * (attempt + 1))
    raise RuntimeError("text-to-speech failed")


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


# ---------------------------------------------------------------- captions

def ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_text(s):
    return s.replace("\\", "").replace("{", "(").replace("}", ")")


def chunk_words(text, max_words=4):
    chunks, cur = [], []
    for w in text.split():
        cur.append(w)
        if len(cur) >= max_words or (len(cur) >= 2 and re.search(r"[.,!?;:—]$", w)):
            chunks.append(" ".join(cur))
            cur = []
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def build_captions(scenes, title):
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Cap,DejaVu Sans,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,6,3,2,90,90,620,1",
        "Style: Title,DejaVu Sans,72,&H0000D7FF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,1,0,1,6,3,8,80,80,240,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if CFG.get("show_title_card", True) and title:
        end = min(scenes[0]["start"] + scenes[0]["dur"], 4.0)
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(end)},Title,,0,0,0,,"
                     f"{{\\fad(200,300)}}{ass_text(title.upper())}")
    if CFG.get("captions", True):
        for sc in scenes:
            chunks = chunk_words(sc["narration"])
            weights = [len(c) + (6 if re.search(r"[.!?]$", c) else 2 if re.search(r"[,;:—]$", c) else 0)
                       for c in chunks]
            total, t = sum(weights), sc["start"]
            for c, wgt in zip(chunks, weights):
                d = sc["voice_dur"] * wgt / total
                lines.append(f"Dialogue: 0,{ass_time(t)},{ass_time(t + d)},Cap,,0,0,0,,{ass_text(c)}")
                t += d
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- video

def ffmpeg(*args, cwd=None):
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *map(str, args)],
                   check=True, cwd=cwd)


def render_scene(i, image, audio, dur, out):
    frames = max(1, int(round(dur * FPS)))
    center = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    z, (x, y) = [
        (f"1.0+0.14*on/{frames}", center),                                # slow push in
        (f"1.14-0.14*on/{frames}", center),                               # slow pull out
        ("1.12", (f"(iw-iw/zoom)*on/{frames}", "ih/2-(ih/zoom/2)")),       # pan left to right
        ("1.12", (f"(iw-iw/zoom)*(1-on/{frames})", "ih/2-(ih/zoom/2)")),   # pan right to left
    ][i % 4]
    vf = (f"scale=1620:2880:force_original_aspect_ratio=increase,crop=1620:2880,"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},"
          f"eq=contrast=1.06:saturation=0.85,vignette=PI/5,format=yuv420p")
    ffmpeg("-i", image, "-i", audio,
           "-filter_complex", f"[0:v]{vf}[v];[1:a]aresample=44100,apad[a]",
           "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}", "-r", FPS,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-c:a", "aac", "-b:a", "192k", "-ac", "2", out)


def render_video(scenes, title, work, final):
    t = 0.0
    for sc in scenes:
        sc["voice_dur"] = duration(sc["audio"])
        sc["dur"] = sc["voice_dur"] + CFG["scene_gap_seconds"]
        sc["start"] = t
        t += sc["dur"]
    for i, sc in enumerate(scenes):
        log(f"  rendering scene {i + 1}/{len(scenes)}")
        render_scene(i, sc["image"], sc["audio"], sc["dur"], work / f"scene_{i:02d}.mp4")
    (work / "list.txt").write_text("".join(f"file 'scene_{i:02d}.mp4'\n" for i in range(len(scenes))))
    (work / "captions.ass").write_text(build_captions(scenes, title))
    ffmpeg("-f", "concat", "-safe", "0", "-i", "list.txt", "-vf", "ass=captions.ass",
           "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p",
           "-c:a", "copy", "-movflags", "+faststart", final.resolve(), cwd=work)
    return t


# ---------------------------------------------------------------- main

def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "case"


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    for f in WORK.iterdir():
        f.unlink()
    used = load_json(USED_FILE, [])

    script = None
    for title in candidates(set(used))[:5]:
        log(f"Trying: {title}")
        used.append(title)
        try:
            real_title, text, url = article(title)
            if real_title not in used:
                used.append(real_title)
            if len(text) < CFG["min_article_chars"]:
                log("  too short, skipping")
                continue
            script = write_script(real_title, text)
            if script.get("skip"):
                log(f"  skipped by writer: {script.get('reason', '')}")
                script = None
                continue
            break
        except (ValueError, json.JSONDecodeError, requests.RequestException) as e:
            log(f"  failed: {e}")
            script = None
    if not script:
        save_json(USED_FILE, used)  # skipped articles are remembered
        sys.exit("No usable article this run; try again.")
    # Remember skipped articles now, but the chosen case only once its video is finished,
    # so a failed run doesn't use it up.
    save_json(USED_FILE, [t for t in used if t not in (title, real_title)])

    log(f"Making video: {script.get('title')} ({real_title})")
    seed = random.randint(1, 10**8)
    scenes = []
    for i, s in enumerate(script["scenes"]):
        log(f"  image + voice {i + 1}/{len(script['scenes'])}")
        image, audio = WORK / f"scene_{i:02d}.jpg", WORK / f"scene_{i:02d}.mp3"
        make_image(s["image_prompt"], image, seed + i * 10)
        asyncio.run(make_voice(s["narration"], audio))
        scenes.append({"narration": s["narration"], "image": image, "audio": audio})

    final = OUT / "video.mp4"
    total = render_video(scenes, script.get("title", ""), WORK, final)
    log(f"Video ready: {total:.1f} seconds, {final.stat().st_size / 1e6:.1f} MB")
    save_json(USED_FILE, used)

    tags = list(dict.fromkeys([t.strip().lstrip("#").lower().replace(" ", "")
                               for t in script.get("hashtags", []) + CFG.get("extra_hashtags", [])
                               if t.strip()]))
    caption = (f"{script.get('caption', '').strip()}\n\n"
               + " ".join("#" + t for t in tags)
               + f"\n\nSource: Wikipedia, \"{real_title}\" (CC BY-SA 4.0)")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    save_json(OUT / "meta.json", {
        "date": today,
        "slug": f"{today}-{slugify(real_title)}",
        "title": script.get("title", real_title),
        "wikipedia_title": real_title,
        "wikipedia_url": url,
        "caption": caption,
        "seconds": round(total, 1),
        "narration": [s["narration"] for s in scenes],
    })


if __name__ == "__main__":
    main()
