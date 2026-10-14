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

SKIP_TITLES = re.compile(r"^(List|Lists|Timeline|Index|Outline) of|\((\d{4} )?(film|TV series|television series|miniseries|book|novel|album|song|documentary|play|musical|video game|podcast|opera|TV film|episode)\)$", re.I)
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


FICTION_CATS = re.compile(
    r"fiction|characters|films|television|tv series|novels|books|comics|manga|anime|video games|"
    r"songs|albums|plays|operas|musicals|podcasts|documentar|in popular culture|mythology|folklore|legends",
    re.I)


def is_fiction_or_media(title):
    """True if Wikipedia files the article under fictional characters, films, TV, books, etc."""
    data = wiki({"action": "query", "prop": "categories", "titles": title, "cllimit": "max",
                 "clshow": "!hidden", "redirects": "1"})
    cats = [c["title"] for p in data.get("query", {}).get("pages", []) for c in p.get("categories", [])]
    hits = [c for c in cats if FICTION_CATS.search(c)]
    if hits:
        log(f"  not a real case ({hits[0]}), skipping")
    return bool(hits)


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
            elif m["ns"] == 14 and not FICTION_CATS.search(m["title"]):
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

Storytelling style: like popular short-form true-crime storytellers such as Ray William Johnson. Talk straight to the viewer as if explaining a fascinating case to a friend, and walk through it step by step. Pack it with specifics from the article: full names, ages, exact dates and times, towns and street names, distances, amounts of money, what evidence was found, and what investigators did next. Every scene must add a new concrete fact that moves the story forward. Keep it serious and respectful, never jokey or melodramatic.
Do NOT use cliffhanger cliches such as "until...", "little did they know", "everything changed", "but that's when", "you won't believe" or "what happened next".

Language: keep it PG and TikTok-safe. Never use any of these words: {avoid_words}. Use calm alternatives instead, for example "the person responsible", "the suspect", "the offender", "lost her life", "was found deceased", "the crime", "the attack", "the weapon", "took his own life". Never describe injuries.

Facts:
- Stick strictly to facts in the article. Never invent details, dialogue, thoughts or motives.
- Anyone not convicted is a "suspect" or "accused"; keep the article's "allegedly" wording. Never name a living person as guilty unless the article says they were convicted.
- Be respectful toward victims and their families. No mockery, no jokes.
- If the article is not about a specific REAL criminal case or real criminal (for example it is about a fictional character, a film, TV show, book or game), is too thin to tell a story, or centers on sexual abuse of a child, return {"skip": true, "reason": "..."} and nothing else.

Otherwise return JSON exactly like:
{
  "skip": false,
  "title": "short on-screen title, max 6 words",
  "scenes": [ { "narration": "...", "image_prompt": "...", "sfx": "" } ],
  "caption": "...",
  "hashtags": ["..."]
}
- {scenes_min} to {scenes_max} scenes. Scene 1 is the hook: state the single most striking specific fact of the case plainly (who, where, when, and the strange detail), as one or two sentences. Then tell the story in order: background, what happened, the investigation and evidence, key turning points, suspects or trial. The last scene says how the case ended or where it stands today, then "Follow for more cases."
- Total narration {words_min} to {words_max} words. Short spoken sentences. Write numbers, dates and years the way they are read aloud. No emojis, no stage directions.
- image_prompt: describe the CONTENT of ONE bright, clear comic-book panel for that moment in plain visual terms (the art style is added automatically): places, buildings, rooms, objects, vehicles, weather, era-appropriate details, people seen from behind or far away, dramatic camera angles. Prefer daytime or brightly lit scenes. Never describe a real person's face, never violence, blood, bodies, injuries or weapons being used, never text, speech bubbles, signs, newspapers or logos.
- sfx: comic-book sound effects for about one scene in three, otherwise an empty string. One or two words in capitals ending with "!" or "...", for an everyday sound that fits that moment, e.g. "RIIING!", "SLAM!", "SCREECH!", "WHOOSH!", "KNOCK KNOCK!", "CLICK!", "FLASH!", "BAM!" (gavel), "WEE-OOO!" (siren), "CRASH!", "THUD!". Never use one for a gunshot, a blow, or the moment someone is harmed.
- title, caption, sfx and hashtags follow the same language rules. caption: under 150 characters, intriguing, no spoilers. hashtags: 4 to 6, lowercase, without the # sign."""


def avoid_regex():
    words = sorted(CFG.get("avoid_words", []), key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(map(re.escape, words)) + r")\b", re.I) if words else None


def bad_words(script):
    rx = avoid_regex()
    if not rx:
        return []
    texts = ([script.get("title", ""), script.get("caption", "")]
             + [s["narration"] for s in script["scenes"]] + [str(s.get("sfx") or "") for s in script["scenes"]])
    return sorted({m.group(0).lower() for t in texts for m in rx.finditer(t)})


SOFTEN = [
    (r"\bserial (killer|murderer)(s?)\b", r"serial offender\2"),
    (r"\b(killer|murderer)(s?)\b", r"perpetrator\2"),
    (r"\b(murdered|killed)\b", "unalived"),
    (r"\b(shot|stabbed|strangled)\b", "attacked"),
    (r"\b(murder|killing|shooting|stabbing)s\b", "crimes"),
    (r"\b(murder|killing|shooting|stabbing)\b", "crime"),
    (r"\bshootout\b", "standoff"),
    (r"\bguns\b", "weapons"), (r"\bgun\b", "weapon"),
    (r"\bfound dead\b", "found deceased"), (r"\bdead\b", "deceased"),
    (r"\bsuicide\b", "taking his own life"),
]


def soften(text):
    for pattern, repl in SOFTEN:
        text = re.sub(pattern, repl, text, flags=re.I)
    return text


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
                time.sleep(15 * (attempt + 1))
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
    global CF_AVAILABLE
    if not (CF_AVAILABLE and CF_ACCOUNT and CF_TOKEN):
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
        if quota_exhausted(r):
            CF_AVAILABLE = False
            return None
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
    system = SYSTEM_PROMPT.replace("{avoid_words}", ", ".join(CFG.get("avoid_words", [])))
    for key in ("scenes_min", "scenes_max", "words_min", "words_max"):
        system = system.replace("{" + key + "}", str(CFG[key]))
    article_part = f"Article title: {title}\n\n{text[:20000]}"
    user = article_part
    best, best_score = None, None
    for attempt in range(3):
        raw = call_gemini(system, user) or call_cloudflare_text(system, user)
        if raw is None:
            sys.exit("No script writer was available (Gemini busy and Cloudflare allowance used up). Try again later.")
        script = parse_json(raw)
        if script.get("skip"):
            return script
        scenes = [s for s in script.get("scenes", [])
                  if isinstance(s, dict) and s.get("narration", "").strip() and s.get("image_prompt", "").strip()]
        if len(scenes) < 5:
            log(f"  script has only {len(scenes)} usable scenes")
            continue
        script["scenes"] = scenes[: CFG["scenes_max"] + 2]
        words = sum(len(s["narration"].split()) for s in script["scenes"])
        banned = bad_words(script)
        long_enough = words >= CFG["words_min"] * 0.9
        log(f"  {len(script['scenes'])} scenes, {words} words" + (f", avoid-words used: {banned}" if banned else ""))
        score = (long_enough, not banned, words)
        if best_score is None or score > best_score:
            best, best_score = script, score
        if long_enough and not banned:
            break
        fixes = []
        if not long_enough:
            fixes.append(f"Your last script was only {words} words. The total narration MUST be "
                         f"{CFG['words_min']} to {CFG['words_max']} words: add more specific true details "
                         f"from the article (names, dates, places, evidence, investigation, aftermath).")
        if banned:
            fixes.append(f"Your last script used these words, which are not allowed: {', '.join(banned)}. "
                         f"Rewrite without them, using calm PG alternatives.")
        log("  asking for a rewrite")
        user = "IMPORTANT: " + " ".join(fixes) + "\n\n" + article_part
    if best is None:
        raise ValueError("no usable script")
    if not best_score[0]:
        raise ValueError(f"script still too short ({best_score[2]} words), trying another case")
    if not best_score[1]:  # last resort: swap the remaining words for PG ones
        log(f"  softening remaining words: {bad_words(best)}")
        best["title"] = soften(best.get("title", ""))
        best["caption"] = soften(best.get("caption", ""))
        rx = avoid_regex()
        for sc in best["scenes"]:
            sc["narration"] = soften(sc["narration"])
            if rx and rx.search(str(sc.get("sfx") or "")):
                sc["sfx"] = ""
    return best


# ---------------------------------------------------------------- media

SAFE_PROMPT = "an empty rain-soaked street at night under a flickering streetlight, distant police lights"


QUOTA_MSG = ("Cloudflare's free daily AI allowance is used up, so no pictures or voice can be made right now. "
             "It resets at 00:00 UTC (8pm New York in summer, 7pm in winter); the next scheduled run will try again.")


CF_AVAILABLE = True  # flips to False once Cloudflare's free daily allowance runs out


def quota_exhausted(r):
    return r.status_code == 429 and "daily free allocation" in r.text


def cloudflare_quota_ok():
    """Tiny, near-free request to see if today's Cloudflare allowance is still available."""
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/@cf/deepgram/aura-1"
    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {CF_TOKEN}"},
                          json={"text": "Hi.", "speaker": "zeus"}, timeout=60)
    except requests.RequestException:
        return True  # network blip: carry on and let the real calls decide
    return not quota_exhausted(r)


_LOCAL_SD = None


def local_image(prompt, path, seed):
    """Comic-style picture made on the GitHub machine itself (no limits, slower, simpler)."""
    global _LOCAL_SD
    import torch
    from diffusers import AutoPipelineForText2Image, LCMScheduler
    if _LOCAL_SD is None:
        log("  loading the local picture generator (first time takes a few minutes)")
        torch.set_num_threads(os.cpu_count() or 4)
        pipe = AutoPipelineForText2Image.from_pretrained(CFG["local_image_model"], torch_dtype=torch.float32)
        pipe.scheduler = LCMScheduler.from_config(pipe.scheduler.config)
        pipe.load_lora_weights(CFG["local_image_lora"])
        pipe.fuse_lora()
        pipe.set_progress_bar_config(disable=True)
        _LOCAL_SD = pipe
    style = CFG.get("local_image_style", CFG["image_style"])
    for attempt in range(3):
        base = prompt if attempt < 2 else SAFE_PROMPT
        out = _LOCAL_SD(prompt=f"{base}, {style}"[:900],
                        negative_prompt="text, letters, words, watermark, signature, speech bubble, photo, "
                                        "blurry, deformed, extra limbs, gore, blood, nsfw, nude",
                        num_inference_steps=CFG.get("local_image_steps", 6), guidance_scale=1.5,
                        width=512, height=768, generator=torch.Generator().manual_seed(seed + attempt))
        flagged = (getattr(out, "nsfw_content_detected", None) or [False])[0]
        if not flagged:
            out.images[0].convert("RGB").save(path, "JPEG", quality=95)
            return
        log("  local picture flagged by the safety checker, trying again")
    raise RuntimeError("local picture generator could not make a safe image")


def make_image(prompt, path, seed):
    global CF_AVAILABLE
    if CF_AVAILABLE and CF_ACCOUNT and CF_TOKEN:
        try:
            if cloudflare_image(prompt, path, seed):
                return
        except QuotaExhausted:
            log("  Cloudflare's free allowance ran out; switching to the local picture generator")
            CF_AVAILABLE = False
    local_image(prompt, path, seed)


class QuotaExhausted(Exception):
    pass


def cloudflare_image(prompt, path, seed):
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/{CFG['cloudflare_image_model']}"
    for attempt in range(5):
        base = prompt if attempt < 3 else SAFE_PROMPT  # last tries: something that can't be refused
        full = f"{base}, {CFG['image_style']}"[:2000]
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {CF_TOKEN}"},
                              json={"prompt": full, "steps": CFG["image_steps"]},
                              timeout=180)
            if quota_exhausted(r):
                raise QuotaExhausted()
            image = r.json().get("result", {}).get("image") if r.ok else None
            if image:
                path.write_bytes(base64.b64decode(image))
                return True
            log(f"  image attempt {attempt + 1} failed: {r.status_code} {r.text[:200]}")
        except requests.RequestException as e:
            log(f"  image attempt {attempt + 1} failed: {e}")
        time.sleep(5 * (attempt + 1))
    log("  Cloudflare could not make this picture; using the local generator")
    return False


def cloudflare_voice(text, path):
    """Narration from Cloudflare Workers AI (Deepgram Aura voices), free daily allowance."""
    global CF_AVAILABLE
    if not (CF_AVAILABLE and CF_ACCOUNT and CF_TOKEN):
        return False
    for model in CFG["cloudflare_voice_models"]:
        url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/{model}"
        for attempt in range(3):
            try:
                r = requests.post(url, headers={"Authorization": f"Bearer {CF_TOKEN}"},
                                  json={"text": text, "speaker": CFG["cloudflare_voice"]}, timeout=180)
            except requests.RequestException as e:
                log(f"  voice ({model}) attempt {attempt + 1} failed: {e}")
                time.sleep(5 * (attempt + 1))
                continue
            if quota_exhausted(r):
                CF_AVAILABLE = False
                return False
            if r.ok and "json" not in r.headers.get("content-type", "") and len(r.content) > 2000:
                path.write_bytes(r.content)
                return True
            if r.ok:  # some models answer with base64 audio inside JSON
                audio = (r.json().get("result") or {}).get("audio")
                if audio:
                    path.write_bytes(base64.b64decode(audio))
                    return True
            log(f"  voice ({model}) attempt {attempt + 1} failed: {r.status_code} {r.text[:200]}")
            if r.status_code == 400:
                break  # this model doesn't accept the request; try the next one
            time.sleep(5 * (attempt + 1))
    return False


async def edge_voice(text, path):
    """Narration from Microsoft's free Edge voices (sometimes blocked on GitHub's machines)."""
    try:
        import edge_tts
    except ImportError:
        return False
    for attempt in range(2):
        try:
            await edge_tts.Communicate(text, CFG["voice"], rate=CFG["voice_rate"]).save(str(path))
            if path.exists() and path.stat().st_size > 2000:
                return True
        except Exception as e:  # noqa: BLE001
            log(f"  edge voice attempt {attempt + 1} failed: {str(e)[:120]}")
        await asyncio.sleep(5)
    return False


_KOKORO = None


def kokoro_voice(text, path):
    """Narration made on the GitHub machine itself with the free Kokoro voice model (no limits)."""
    global _KOKORO
    try:
        import numpy as np
        import soundfile as sf
        from kokoro import KPipeline
        if _KOKORO is None:
            _KOKORO = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
        chunks = []
        for _, _, audio in _KOKORO(text, voice=CFG["kokoro_voice"], speed=CFG.get("kokoro_speed", 1.0)):
            if audio is not None:
                chunks.append(audio.cpu().numpy() if hasattr(audio, "cpu") else np.asarray(audio))
        if not chunks:
            return False
        sf.write(str(path), np.concatenate(chunks), 24000)
        return path.stat().st_size > 2000
    except Exception as e:  # noqa: BLE001
        log(f"  local voice failed: {str(e)[:200]}")
        return False


def make_voice(text, base):
    """Writes the narration next to `base` (no extension) and returns the file it wrote."""
    for engine in CFG.get("voice_engines", ["kokoro", "cloudflare", "edge"]):
        if engine == "kokoro":
            path = base.with_suffix(".wav")
            ok = kokoro_voice(text, path)
        elif engine == "cloudflare":
            path = base.with_suffix(".mp3")
            ok = cloudflare_voice(text, path)
        else:
            path = base.with_suffix(".mp3")
            ok = asyncio.run(edge_voice(text, path))
        if ok:
            return path
    raise RuntimeError("text-to-speech failed with every voice service")


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
        # Comic look: yellow narration boxes with black text, punchy title and sound-effect lettering.
        "Style: Cap,Comic Neue,78,&H00000000,&H00000000,&H0099F5FF,&H00000000,-1,0,0,0,100,100,0,0,3,16,0,2,90,90,560,1",
        "Style: Title,Bangers,104,&H0000E1FF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,3,0,1,8,6,8,70,70,230,1",
        "Style: SFX,Bangers,190,&H0000E1FF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,4,0,1,11,8,5,40,40,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if CFG.get("show_title_card", True) and title:
        end = min(scenes[0]["start"] + scenes[0]["dur"], 4.0)
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(end)},Title,,0,0,0,,"
                     f"{{\\fad(200,300)}}{ass_text(title.upper())}")
    end = scenes[-1]["start"] + scenes[-1]["dur"]
    lines.append(f"Dialogue: 1,{ass_time(end - 3.0)},{ass_time(end)},Title,,0,0,0,,"
                 "{\\fad(300,0)}FOLLOW FOR MORE CASES")
    for i, sc in enumerate(s for s in scenes if s.get("sfx")):
        x, angle = (330, 10) if i % 2 == 0 else (750, -10)
        start = sc["start"] + 0.15
        lines.append(
            f"Dialogue: 2,{ass_time(start)},{ass_time(min(start + 1.4, sc['start'] + sc['dur']))},SFX,,0,0,0,,"
            f"{{\\pos({x},560)\\frz{angle}\\fscx30\\fscy30\\t(0,140,\\fscx120\\fscy120)"
            f"\\t(140,260,\\fscx100\\fscy100)\\fad(0,250)}}{ass_text(sc['sfx'])}")
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
          f"eq=brightness=0.02:contrast=1.04:saturation=1.05,format=yuv420p")
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
    # Guarantee the minimum length: hold the last picture a little longer if needed.
    if t < CFG.get("min_seconds", 61):
        log(f"  {t:.1f}s is under {CFG.get('min_seconds', 61)}s, extending the ending")
        scenes[-1]["dur"] += CFG.get("min_seconds", 61) - t
        t = CFG.get("min_seconds", 61)
    for i, sc in enumerate(scenes):
        log(f"  rendering scene {i + 1}/{len(scenes)}")
        render_scene(i, sc["image"], sc["audio"], sc["dur"], work / f"scene_{i:02d}.mp4")
    (work / "list.txt").write_text("".join(f"file 'scene_{i:02d}.mp4'\n" for i in range(len(scenes))))
    (work / "captions.ass").write_text(build_captions(scenes, title))
    ffmpeg("-f", "concat", "-safe", "0", "-i", "list.txt", "-vf", f"ass=captions.ass:fontsdir={ROOT / 'fonts'}",
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
    global CF_AVAILABLE
    CF_AVAILABLE = bool(CF_ACCOUNT and CF_TOKEN) and cloudflare_quota_ok()
    if not CF_AVAILABLE:
        log("Cloudflare's free allowance is not available right now; using the local picture generator.")

    script = None
    for title in candidates(set(used))[:5]:
        log(f"Trying: {title}")
        used.append(title)
        try:
            real_title, text, url = article(title)
            if real_title not in used:
                used.append(real_title)
            if is_fiction_or_media(real_title):
                continue
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
        image = WORK / f"scene_{i:02d}.jpg"
        make_image(s["image_prompt"], image, seed + i * 10)
        audio = make_voice(s["narration"], WORK / f"scene_{i:02d}")
        sfx = str(s.get("sfx") or "").strip().upper()[:20] if CFG.get("sfx", True) else ""
        scenes.append({"narration": s["narration"], "sfx": sfx, "image": image, "audio": audio})

    final = OUT / "video.mp4"
    total = render_video(scenes, script.get("title", ""), WORK, final)
    if total < CFG.get("min_seconds", 61) - 0.5:
        sys.exit(f"Video is only {total:.1f}s; not posting a video under the minimum length.")
    log(f"Video ready: {total:.1f} seconds, {final.stat().st_size / 1e6:.1f} MB")
    save_json(USED_FILE, used)

    avoid = [w.lower() for w in CFG.get("avoid_words", [])]
    tags = list(dict.fromkeys([t.strip().lstrip("#").lower().replace(" ", "")
                               for t in script.get("hashtags", []) + CFG.get("extra_hashtags", [])
                               if t.strip()]))
    tags = [t for t in tags if not any(w in t for w in avoid)]
    rx = avoid_regex()
    source = "Wikipedia" if rx and rx.search(real_title) else f"Wikipedia, \"{real_title}\""
    caption = (f"{script.get('caption', '').strip()}\n\n"
               + " ".join("#" + t for t in tags)
               + f"\n\nSource: {source} (CC BY-SA 4.0)")
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
