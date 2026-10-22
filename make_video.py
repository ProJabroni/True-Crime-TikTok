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

from spoken import spoken

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
# Wars, military operations, terrorism and political conflicts: not the channel's kind of story.
SENSITIVE_CATS = re.compile(
    r"military operations|\bwars?\b|battles|world war|prisoners of war|conflict|terroris|militant|insurgen|"
    r"guerrilla|genocide|holocaust|coups|intifada|occupation|armed forces|military personnel|"
    r"generals|soldiers of|resistance members",
    re.I)


def excluded_cat(title):
    return FICTION_CATS.search(title) or SENSITIVE_CATS.search(title)


US_STATES = ("Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia \\(U\\.S\\. state\\)|"
             "Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|"
             "Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|"
             "North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|"
             "South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|"
             "Washington, D\\.C\\.|Los Angeles|Chicago|Houston|Philadelphia|Boston|Detroit|Seattle|San Francisco")
EUROPE = ("Europe|European|United Kingdom|British|England|English|Scotland|Scottish|Wales|Welsh|Northern Ireland|"
          "Ireland|Irish|Germany|German|France|French|Italy|Italian|Spain|Spanish|Portugal|Portuguese|Netherlands|Dutch|"
          "Belgium|Belgian|Luxembourg|Switzerland|Swiss|Austria|Austrian|Denmark|Danish|Norway|Norwegian|Sweden|"
          "Swedish|Finland|Finnish|Iceland|Icelandic|Poland|Polish|Czech|Slovakia|Slovak|Hungary|Hungarian|Romania|"
          "Romanian|Bulgaria|Bulgarian|Greece|Greek|Croatia|Croatian|Slovenia|Slovenian|Serbia|Serbian|Estonia|"
          "Estonian|Latvia|Latvian|Lithuania|Lithuanian|Malta|Maltese|Cyprus|Cypriot|London|Paris|Berlin|Rome|"
          "Madrid|Amsterdam|Dublin|Glasgow|Edinburgh|Manchester|Liverpool|Birmingham|Munich|Hamburg|Vienna")
PLACE_RX = re.compile(r"(?<!\w)(United States|U\.S\.|American|" + US_STATES + "|" + EUROPE + r")(?!\w)")
MURDER_RX = re.compile(r"murder|homicide|serial killer|manslaughter|people killed|deaths by (stabbing|strangulation|"
                       r"firearm|blunt|poison|asphyxia|drowning|beating)|killings in", re.I)


DARK_CATS = re.compile(
    r"child (sexual )?abuse|crimes against children|violence against children|murdered (american |english |british |"
    r"german |french )?(children|girls|boys|infants)|child murder|infanticide|filicide|rapists|\brape\b|sexual (abuse|"
    r"assault|violence)|sex offenders|sex crimes|paedophil|pedophil|child pornography|necrophil|cannibal",
    re.I)
DARK_TEXT = re.compile(r"\b(rap(e|ed|es|ing|ist)|molest\w*|sexual(ly)? (abus|assault)\w*|paedophil\w*|pedophil\w*)\b", re.I)


def too_dark(cats, text):
    """Cases centred on sexual violence or child victims are not for this channel."""
    hit = next((c for c in cats if DARK_CATS.search(c)), None)
    if hit:
        return hit
    if len(DARK_TEXT.findall(text)) >= CFG.get("dark_word_limit", 2):
        return "sexual violence in the article"
    return None


def check_case(title):
    """True if the article is a real murder case in the United States or Europe (judged by its categories)."""
    data = wiki({"action": "query", "prop": "categories", "titles": title, "cllimit": "max",
                 "clshow": "!hidden", "redirects": "1"})
    cats = [c["title"] for p in data.get("query", {}).get("pages", []) for c in p.get("categories", [])]
    hits = [c for c in cats if excluded_cat(c)]
    if hits:
        log(f"  not the right kind of case ({hits[0]}), skipping")
        return False
    if CFG.get("murder_cases_only", True) and not any(MURDER_RX.search(c) for c in cats):
        log("  not a murder case, skipping")
        return False
    if CFG.get("us_and_europe_only", True) and not any(PLACE_RX.search(c) for c in cats):
        log("  not in the United States or Europe, skipping")
        return False
    return cats


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
            elif m["ns"] == 14 and not excluded_cat(m["title"]):
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
    groups = CFG.get("category_groups") or {"all": CFG.get("categories", [])}
    names = list(groups)
    random.shuffle(names)  # each region equally likely, whatever the number of categories in it
    cats = []
    for name in names:
        group = list(groups[name])
        random.shuffle(group)
        cats += group
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
- If the case involves sexual violence or victims who were children, return {"skip": true, "reason": "..."}.
- If the article is not about a specific REAL criminal case or real criminal (for example it is about a fictional character, a film, TV show, book or game), is not about a murder (or suspected murder) that happened in the United States or Europe, is mainly about a war, military operation, terrorism or a political or ethnic conflict, is too thin to tell a story, or centers on sexual abuse of a child, return {"skip": true, "reason": "..."} and nothing else.

Otherwise return JSON exactly like:
{
  "skip": false,
  "title": "short on-screen title that teases the mystery, max 7 words",
  "setting": { "when": "the date the story begins, as exact as the article gives it, always with the year", "where": "the town or city plus state or country where it happened" },
  "setting": { "country": "...", "place": "...", "era": "..." },
  "characters": [ { "tag": "[Victim]", "look": "..." } ],
  "scenes": [ { "narration": "...", "image_prompt": "..." } ],
  "caption": "...",
  "hashtags": ["..."]
}
- {scenes_min} to {scenes_max} scenes.
- Scene 1 is the HOOK, and it decides whether people keep watching. It is ONE short, intriguing line (under 18 words), usually a question, built on the strangest or most surprising TRUE detail in the article, that makes the viewer need the answer. Tease the mystery; do not give away the ending, and never exaggerate or mislead. Good hooks for the style (do not copy them): "How was this man able to hitch a ride out of prison?", "Why did a quiet bank clerk walk out with a million dollars and never come back?", "What were police looking for under a suburban kitchen floor for forty years?". Avoid weak openers such as "This is the story of", "Meet", "Have you ever heard of", "You won't believe".
- Scene 2 starts the story properly with who, where and when: it MUST say out loud the date (always the year, plus the month and day when the article gives them) and the place (the town or city and the state or country). The viewer must know when and where this happened within the first half of the video, so never save the date or place for later. Then tell the story in order: background, what happened, the investigation and evidence, key turning points, suspects or trial. The last scene says how the case ended or where it stands today. Do not add any "like", "follow" or "subscribe" line yourself; the closing call to action is added automatically.
- Total narration {words_min} to {words_max} words. Short spoken sentences. No emojis, no stage directions.
- NEVER use digits in narration. Spell out every number, date, year, time and amount exactly as a narrator says it: "August thirty-first, nineteen eighty-five", "the third of March", "seven thirty in the evening", "twenty-three years old", "one point five million dollars".
- setting: where and when the case happened, from the article: country (e.g. "Germany"), place (city or region, e.g. "a village near Munich, Bavaria") and era (e.g. "1940s"). Every picture is drawn in this setting.
- characters: the 2 to 5 key people in the story (victim, suspect, detective, witness, family member...). tag is a short role in square brackets, e.g. "[Victim]", "[Detective]", "[Suspect]". look is a fixed visual description used in every picture: gender, approximate age, build, hair, and clothing typical of the era and place (e.g. "a woman in her twenties with shoulder-length brown hair, wearing a 1970s denim jacket and flared jeans"). Do not try to describe the real person's actual face; these are illustrated characters.
- Use the [Tag] form ONLY inside image_prompt. In narration, title and caption, write normally (names or "the detective").
- image_prompt: ONE bright, clear, SIMPLE illustrated picture for that moment (the art style is added automatically). Less is more: in most scenes show just one thing (one person, one object or one place) against a plain, uncluttered background, like a single key, a lit window at night, a car on an empty road, a face in shadow, a closed door. Keep each image_prompt under 25 words and never list many objects. Each picture shows AT MOST ONE PERSON: a single character, alone in the frame. Never two or more people, no groups, crowds or bystanders; when a moment involves several people, show just the most important one, or the place or an object. Most scenes should SHOW one of the characters, referred to ONLY by their tag (e.g. "[Detective] kneels beside a car on a rainy roadside, looking at the ground"), doing what the story says at that moment, with the setting, era, time of day and camera angle. Never put a real person's name in image_prompt. Show people alive and in ordinary or tense moments only: never violence, attacks, weapons being used, blood, injuries, bodies or crime-scene remains; for those moments show the place, a detail, or the investigators instead. Never show a child in danger. Everything in the picture must fit the real country and era of the case: local architecture, streets, transport (horse-drawn carriages before about 1900, never cars), lighting, police uniforms, courtrooms and clothing (e.g. a German case gets German buildings, German police and a German courtroom, never American ones). Never show flags. Avoid anything that normally carries writing: signs, shop fronts, newspapers, documents, screens, license plates, badges, posters, uniforms with lettering.
- title, caption and hashtags follow the same language rules. caption: under 150 characters, intriguing, no spoilers. hashtags: 4 to 6, lowercase, without the # sign."""


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


class CaseRefused(ValueError):
    """The script writer's safety filter refused this article: skip the case entirely."""


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
        data = r.json()
        block = (data.get("promptFeedback") or {}).get("blockReason")
        finish = ((data.get("candidates") or [{}])[0]).get("finishReason", "")
        if block or finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
            raise CaseRefused(f"Gemini's safety filter refused this case ({block or finish})")
        try:
            parts = data["candidates"][0]["content"]["parts"]
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


_TEENS = "eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen"
_TENS = "ten|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
YEAR_RX = re.compile(  # a year written in digits or read aloud ("nineteen ninety-four", "two thousand and three")
    rf"\b(1\d{{3}}|20\d{{2}}|two thousand|(?:{_TEENS}|twenty)[ -](?:hundred|oh|o|{_TEENS}|{_TENS}))\b", re.I)


def missing_setting(script):
    """What the first half of the narration still needs: the year and/or the place."""
    scenes = script["scenes"]
    first_half = " ".join(s["narration"] for s in scenes[: (len(scenes) + 1) // 2])
    missing = []
    if not YEAR_RX.search(first_half):
        missing.append("date")
    where = str((script.get("setting") or {}).get("where") or "")
    place_words = [w for w in re.findall(r"[A-Z][\w'.-]{2,}", where)
                   if w.lower() not in {"the", "county", "city", "state", "united", "states", "republic"}]
    if not place_words or not any(re.search(rf"\b{re.escape(w)}\b", first_half, re.I) for w in place_words):
        missing.append("place")
    return missing


CTA_RX = re.compile(r"\b(follow|subscribe)\b.*\bfor more\b|^(and )?(like|follow|subscribe|smash)\b", re.I)


def add_closing_line(script):
    """Every video ends with the same spoken like-and-follow line (any the writer added is replaced)."""
    last = script["scenes"][-1]
    sentences = re.split(r"(?<=[.!?])\s+", last["narration"].strip())
    kept = [x for x in sentences if not CTA_RX.search(x.strip())]
    closing = CFG.get("closing_line", "Like and follow for more cases.")
    last["narration"] = " ".join(kept + [closing]).strip()
    return script


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
        hook_words = len(script["scenes"][0]["narration"].split())
        hook_ok = hook_words <= 25
        no_setting = missing_setting(script)
        log(f"  {len(script['scenes'])} scenes, {words} words" + (f", avoid-words used: {banned}" if banned else "")
            + (f", first half is missing the {' and '.join(no_setting)}" if no_setting else ""))
        score = (long_enough, not banned, not no_setting, hook_ok, words)
        if best_score is None or score > best_score:
            best, best_score = script, score
        if long_enough and not banned and not no_setting and hook_ok:
            break
        fixes = []
        if not long_enough:
            fixes.append(f"Your last script was only {words} words. The total narration MUST be "
                         f"{CFG['words_min']} to {CFG['words_max']} words: add more specific true details "
                         f"from the article (names, dates, places, evidence, investigation, aftermath).")
        if banned:
            fixes.append(f"Your last script used these words, which are not allowed: {', '.join(banned)}. "
                         f"Rewrite without them, using calm PG alternatives.")
        if no_setting:
            fixes.append("Scene 2 must say out loud WHEN this happened (always the year, plus month and day if the "
                         "article gives them) and WHERE (town or city plus state or country), and fill in \"setting\" "
                         f"with the same date and place. Your last script's first half was missing the {' and '.join(no_setting)}.")
        if not hook_ok:
            fixes.append(f"Scene 1 must be ONE short intriguing hook line under 18 words (yours was {hook_words}); "
                         "move the details into scene 2.")
        log("  asking for a rewrite")
        user = "IMPORTANT: " + " ".join(fixes) + "\n\n" + article_part
    if best is None:
        raise ValueError("no usable script")
    if not best_score[0]:
        raise ValueError(f"script still too short ({best_score[-1]} words), trying another case")
    if set(bad_words(best)) & {"rape", "raped", "molested", "sexual", "torture", "tortured", "dismembered",
                               "decapitated", "corpse"}:
        raise ValueError(f"script keeps needing words like {bad_words(best)}; case too dark, trying another")
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


def untag(text):
    """Turn any leftover [Tag] in spoken/visible text into plain words: "[Detective] said" -> "The detective said"."""
    def repl(m):
        role = m.group(1).strip().lower()
        start = m.start() == 0 or text[:m.start()].rstrip()[-1:] in ".!?"
        return ("The " if start else "the ") + role
    return re.sub(r"\[([^\]]{1,30})\]", repl, text)


def era_year(era):
    """'1836' / 'the 1830s' / 'mid-1800s' / '19th century' -> a representative year (or None)."""
    era = str(era or "")
    m = re.search(r"\b(1[0-9]{3}|20[0-9]{2})", era)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(1[0-9]|20)(?:th|st|nd|rd) century", era, re.I)
    if m:
        return (int(m.group(1)) - 1) * 100 + 50
    return None


def era_details(year, country):
    """A short period hint. Kept brief on purpose: listing props (carriages, lamps...) makes the image
    model cram them into every picture, and the pictures should be simple."""
    if year is None:
        return ""
    decade = year // 10 * 10
    if year < 1900:
        out = f"{decade}s, horse-and-carriage era, period-accurate"
    elif year < 2000:
        out = f"{decade}s, period-accurate"
    else:
        out = "present day"
    if country:
        out += f", {country}"
    return out


def setting_parts(setting):
    """(prefix, details) for the picture prompt, e.g. ('London, England, 1836', 'horse-drawn carriages ...')."""
    if not isinstance(setting, dict):
        return "", ""
    country = str(setting.get("country") or "").strip()
    place = str(setting.get("place") or "").strip()
    era = str(setting.get("era") or "").strip()
    where = place if (country and country.lower() in place.lower()) else ", ".join(x for x in (place, country) if x)
    prefix = ", ".join(x for x in (where, era) if x)
    return prefix, era_details(era_year(era), country)


def picture_prompt(scene_prompt, characters, setting):
    """Final picture prompt: setting and era first (models weigh the start most), one person, period details."""
    prefix, details = setting_parts(setting)
    scene = expand_characters(scene_prompt, characters)
    parts = [prefix, scene, details]
    return ". ".join(x.strip().rstrip(".") for x in parts if x and x.strip())


def expand_characters(prompt, characters):
    """Replace [Tag] with that character's fixed look so they look the same in every picture.
    Only one person per picture: if several tags appear, the first one stays and the others are dropped."""
    tags = list(dict.fromkeys(re.findall(r"\[[^\]]{1,30}\]", prompt)))
    for extra in tags[1:]:
        prompt = re.sub(r"\s*(?:,|\band\b|\bwith\b|\bwhile\b)?\s*" + re.escape(extra), "", prompt)
    if tags:
        prompt += ", a single figure alone, empty surroundings"
    else:
        prompt += ", empty surroundings"
    for ch in characters or []:
        tag, look = str(ch.get("tag", "")).strip(), str(ch.get("look", "")).strip()
        if tag and look:
            if not tag.startswith("["):
                tag = f"[{tag.strip('[]')}]"
            prompt = prompt.replace(tag, look)
    return re.sub(r"\[([^\]]{1,30})\]", r"\1", prompt)  # any unknown tag: keep just the role word


def text_in_image(path, boxes=None, min_conf=None):
    """Number of readable words OCR finds in a picture (0 if the OCR tool isn't installed).
    If `boxes` is a list, each word's (left, top, width, height) is appended to it."""
    try:
        out = subprocess.run(["tesseract", str(path), "-", "--psm", "11", "tsv"],
                             capture_output=True, text=True, timeout=60).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    words = 0
    for line in out.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) == 12:
            try:
                conf = float(cols[10])
            except ValueError:
                continue
            word = cols[11].strip()
            if not re.fullmatch(r"[A-Za-z]{3,}", word):
                continue
            # Signs are usually capitals; lowercase "words" are often just texture, so need more certainty.
            upper_min = min_conf if min_conf is not None else CFG.get("ocr_min_confidence", 75)
            if (word.isupper() and conf >= upper_min) or (len(word) >= 4 and conf >= max(85, upper_min)):
                words += 1
                if boxes is not None:
                    boxes.append(tuple(int(float(c)) for c in cols[6:10]))
    return words


_VISION_FAILS = 0


def _parse_boxes(text, w, h):
    """Accept {"boxes": [[y0,x0,y1,x1], ...]}, a bare list, or [{"box_2d": [...]}, ...]."""
    text = text.strip()
    try:
        data = json.loads(text)
    except ValueError:
        i = min([k for k in (text.find("{"), text.find("[")) if k >= 0], default=-1)
        if i < 0:
            raise
        data = json.loads(text[i:max(text.rfind("}"), text.rfind("]")) + 1])
    if isinstance(data, dict):
        data = data.get("boxes", data.get("text_boxes", []))
    boxes = []
    for b in data or []:
        if isinstance(b, dict):
            b = b.get("box_2d") or b.get("box") or b.get("bbox")
        if isinstance(b, (list, tuple)) and len(b) == 4:
            y0, x0, y1, x1 = [max(0.0, min(1000.0, float(v))) for v in b]
            # Ignore empty or implausibly big boxes (a sign is never a fifth of the picture).
            if y1 > y0 and x1 > x0 and (y1 - y0) * (x1 - x0) < 0.2 * 1000 * 1000:
                boxes.append((int(x0 * w / 1000), int(y0 * h / 1000),
                              int((x1 - x0) * w / 1000), int((y1 - y0) * h / 1000)))
    return boxes


def vision_text_boxes(path):
    """Ask Gemini (vision) where any lettering is, including garbled or partial letters that OCR can't read.
    Returns a list of (left, top, width, height) pixel boxes, [] if clean, or None if it couldn't tell
    (then OCR alone decides for this picture). Gives up for the run only after several failures in a row."""
    global _VISION_FAILS
    if not (GEMINI_KEY and CFG.get("vision_text_check", True)) or _VISION_FAILS >= 4:
        return None
    from PIL import Image
    w, h = Image.open(path).size
    prompt = ("This is an illustration. Find ANY text in it: letters, words, numbers, signs, labels, logos, "
              "license plates, writing on paper or screens, even if garbled, partial, tiny or fake. "
              'Reply with JSON only, exactly like {"boxes": [[ymin, xmin, ymax, xmax]]}, coordinates '
              'normalized 0-1000, one box per text area. If there is no text at all, reply {"boxes": []}.')
    body = {"contents": [{"role": "user", "parts": [
                {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(path.read_bytes()).decode()}},
                {"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
    problem = ""
    models = CFG.get("vision_models", ["gemini-3.8-flash", "gemini-3.5-flash-lite"])
    for model in models:
        # The first (best) model gets a quick second chance; don't wait long when it's busy.
        for attempt in range(2 if model == models[0] else 3):
            try:
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                  headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=90)
            except requests.RequestException as e:
                problem = f"{model}: {e}"
                time.sleep(5)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                problem = f"{model}: busy ({r.status_code})"
                time.sleep(5 if model == models[0] else 15 * (attempt + 1))
                continue
            if not r.ok:
                problem = f"{model}: {r.status_code} {r.text[:200]}"
                break
            data = r.json()
            if (data.get("promptFeedback") or {}).get("blockReason"):
                return None  # safety filter declined this picture; OCR decides for it
            try:
                text = "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
                boxes = _parse_boxes(text, w, h)
            except (KeyError, IndexError, ValueError) as e:
                problem = f"{model}: unreadable reply ({e}): {str(data)[:200]}"
                break
            _VISION_FAILS = 0
            time.sleep(CFG.get("vision_pause", 2))
            return boxes
    _VISION_FAILS += 1
    log(f"  picture text check (Gemini vision) failed: {problem[:300]}")
    if _VISION_FAILS >= 4:
        log("  Gemini vision keeps failing; using OCR only for the rest of this video")
    return None


def lettering_boxes(path):
    """Boxes of lettering: Gemini vision (catches garbled text) plus OCR. When vision answered, OCR only
    counts when it is very sure, because it sometimes reads brick walls or textures as letters."""
    seen = vision_text_boxes(path)
    boxes = []
    text_in_image(path, boxes, min_conf=CFG.get("ocr_sure_confidence", 85) if seen is not None else None)
    return boxes + (seen or [])


def remove_lettering(path, boxes):
    """Paint over lettering using the surrounding picture (inpainting), or blur it if OpenCV is missing."""
    if not boxes:
        return
    try:
        import cv2
        import numpy as np
        img = cv2.imread(str(path))
        mask = np.zeros(img.shape[:2], np.uint8)
        for left, top, width, height in boxes:
            pad = max(6, int(0.15 * max(width, height)))
            cv2.rectangle(mask, (max(0, left - pad), max(0, top - pad)),
                          (min(img.shape[1] - 1, left + width + pad), min(img.shape[0] - 1, top + height + pad)), 255, -1)
        out = cv2.inpaint(img, mask, 9, cv2.INPAINT_TELEA)
        out = np.where(mask[..., None] > 0, cv2.GaussianBlur(out, (0, 0), 3), out)  # soften the patch
        cv2.imwrite(str(path), out, [cv2.IMWRITE_JPEG_QUALITY, 95])
        log(f"  painted over {len(boxes)} bits of lettering")
    except ImportError:
        from PIL import Image, ImageFilter
        img = Image.open(path).convert("RGB")
        for left, top, width, height in boxes:
            pad = max(6, height // 2)
            box = (max(0, left - pad), max(0, top - pad), min(img.width, left + width + pad), min(img.height, top + height + pad))
            img.paste(img.crop(box).filter(ImageFilter.GaussianBlur(radius=max(8, height // 2))), box[:2])
        img.save(path, "JPEG", quality=95)
        log(f"  blurred {len(boxes)} bits of lettering")


def make_image(prompt, path, seed):
    """Make a picture with no lettering: check every try (OCR + Gemini vision), remake up to `text_retries`
    times, keep the cleanest, then paint over whatever lettering is left."""
    best = None
    attempt = 0
    while True:
        tmp = path.with_name(f"{path.stem}_try{attempt}{path.suffix}")
        make_image_once(prompt, tmp, seed + attempt * 101)
        boxes = lettering_boxes(tmp)
        area = sum(bw * bh for _, _, bw, bh in boxes)
        if best is None or area < best[0]:
            best = (area, tmp, boxes)
        # The slow on-GitHub generator gets one retry at most.
        retries = CFG.get("text_retries", 3) if CF_AVAILABLE else min(1, CFG.get("text_retries", 3))
        if not boxes or attempt >= retries:
            if boxes:
                log("  every try had some lettering; keeping the cleanest and painting it out")
            break
        attempt += 1
        log(f"  picture has lettering on it ({len(boxes)} spots), making another")
    best[1].replace(path)
    if best[2]:
        remove_lettering(path, best[2])
    # Always take a second, independent look at the kept picture; anything found is painted over.
    remove_lettering(path, lettering_boxes(path))


def make_image_once(prompt, path, seed):
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


def gemini_voice(text, path):
    """Natural narration from Google's Gemini speech models (free tier). Writes a WAV file."""
    import io
    import wave
    if not GEMINI_KEY:
        return False
    body = {
        "input": [{"type": "user_input", "content": [{
            "type": "text", "text": text,
            "annotations": [{"type": "speech_metadata", "style": CFG["gemini_voice_style"]}]}]}],
        "response_format": {"type": "audio", "mime_type": "audio/wav"},
        "generation_config": {"speech_config": [{"voice": CFG["gemini_voice"]}]},
    }

    def find_audio(node):
        if isinstance(node, dict):
            if node.get("type") == "audio" and node.get("data"):
                return node
            if node.get("inlineData", {}).get("data"):
                return {"data": node["inlineData"]["data"], "mime_type": node["inlineData"].get("mimeType", "")}
            node = list(node.values())
        if isinstance(node, list):
            for item in node:
                found = find_audio(item)
                if found:
                    return found
        return None

    for model in CFG["gemini_tts_models"]:
        for attempt in range(4):
            try:
                r = requests.post("https://generativelanguage.googleapis.com/v1beta/interactions",
                                  headers={"x-goog-api-key": GEMINI_KEY},
                                  json={"model": model, **body}, timeout=180)
            except requests.RequestException as e:
                log(f"  Gemini voice ({model}) error: {e}")
                time.sleep(10)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                log(f"  Gemini voice ({model}) busy ({r.status_code}), waiting")
                time.sleep(20 * (attempt + 1))
                continue
            if not r.ok:
                log(f"  Gemini voice ({model}) failed: {r.status_code} {r.text[:200]}")
                break
            audio = find_audio(r.json())
            if not audio:
                log(f"  Gemini voice ({model}) returned no audio")
                break
            raw = base64.b64decode(audio["data"])
            if raw[:4] != b"RIFF":  # headerless 16-bit PCM: wrap it in a WAV header
                buf = io.BytesIO()
                with wave.open(buf, "wb") as w:
                    w.setnchannels(int(audio.get("channels") or 1))
                    w.setsampwidth(2)
                    w.setframerate(int(audio.get("sample_rate") or 24000))
                    w.writeframes(raw)
                raw = buf.getvalue()
            path.write_bytes(raw)
            time.sleep(CFG.get("gemini_voice_pause", 4))  # stay well inside the free per-minute limit
            return path.stat().st_size > 2000
    return False


VOICE_ENGINES = {
    "gemini": (".wav", gemini_voice),
    "kokoro": (".wav", lambda t, p: kokoro_voice(t, p)),
    "cloudflare": (".mp3", lambda t, p: cloudflare_voice(t, p)),
    "edge": (".mp3", lambda t, p: asyncio.run(edge_voice(t, p))),
}


def make_all_voices(texts, bases):
    """Narrate every scene with the SAME voice: if one engine fails part-way, redo all with the next."""
    for engine in CFG.get("voice_engines", ["gemini", "kokoro", "cloudflare", "edge"]):
        ext, fn = VOICE_ENGINES[engine]
        paths = []
        for i, (text, base) in enumerate(zip(texts, bases)):
            path = base.with_suffix(ext)
            if not fn(text, path):
                log(f"  {engine} voice failed on scene {i + 1}; trying the next voice for all scenes")
                break
            paths.append(path)
        if len(paths) == len(texts):
            log(f"  narration voice: {engine}")
            return paths
    raise RuntimeError("text-to-speech failed with every voice service")


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
                 "{\\fad(300,0)}LIKE & FOLLOW\\NFOR MORE CASES")
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
    subs = f"ass=captions.ass:fontsdir={ROOT / 'fonts'}"
    music = make_background_music(t, work / "music.wav")
    if music:
        vol = CFG.get("music_volume", 0.15)
        mix = (f"[0:v]{subs}[v];[0:a]asplit=2[voice][key];[1:a]volume={vol}[m];"
               "[m][key]sidechaincompress=threshold=0.03:ratio=6:attack=40:release=700[duck];"
               "[voice][duck]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[a]")
        ffmpeg("-f", "concat", "-safe", "0", "-i", "list.txt", "-i", music.name, "-filter_complex", mix,
               "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "22",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-movflags", "+faststart",
               final.resolve(), cwd=work)
    else:
        ffmpeg("-f", "concat", "-safe", "0", "-i", "list.txt", "-vf", subs, "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
               "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-movflags", "+faststart", final.resolve(), cwd=work)
    return t


def make_background_music(seconds, path):
    """Low, ominous background music made fresh for each video (see music.py). None if turned off or it fails."""
    if not CFG.get("music", True):
        return None
    try:
        from music import make_music
        make_music(seconds + 0.5, path, seed=random.randint(1, 10**9))
        log("  background music added")
        return path
    except Exception as e:  # noqa: BLE001
        log(f"  background music skipped: {str(e)[:200]}")
        return None


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
    for title in candidates(set(used))[:8]:
        log(f"Trying: {title}")
        used.append(title)
        try:
            real_title, text, url = article(title)
            if real_title not in used:
                used.append(real_title)
            cats = check_case(real_title)
            if not cats:
                continue
            if len(text) < CFG["min_article_chars"]:
                log("  too short, skipping")
                continue
            dark = too_dark(cats, text)
            if dark:
                log(f"  case involves sexual violence or child victims ({dark}), skipping")
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

    add_closing_line(script)
    script["title"] = untag(script.get("title", ""))
    script["caption"] = untag(script.get("caption", ""))
    for sc in script["scenes"]:
        sc["narration"] = spoken(untag(sc["narration"]))  # "August 31" -> "August thirty-first" for the voice
    log(f"Making video: {script.get('title')} ({real_title})")
    seed = random.randint(1, 10**8)
    scenes = []
    for i, s in enumerate(script["scenes"]):
        log(f"  picture {i + 1}/{len(script['scenes'])}")
        image = WORK / f"scene_{i:02d}.jpg"
        make_image(picture_prompt(s["image_prompt"], script.get("characters"), script.get("setting")),
                   image, seed + i * 10)
        sfx = str(s.get("sfx") or "").strip().upper()[:20] if CFG.get("sfx", False) else ""
        scenes.append({"narration": s["narration"], "sfx": sfx, "image": image})
    log("  narrating")
    audios = make_all_voices([sc["narration"] for sc in scenes],
                             [WORK / f"scene_{i:02d}" for i in range(len(scenes))])
    for sc, audio in zip(scenes, audios):
        sc["audio"] = audio

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
