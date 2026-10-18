# True Crime TikTok

Every day this repo makes a narrated true-crime video of over a minute from a Wikipedia
article and sends it to TikTok through Buffer. Everything runs on GitHub's free machines
and free services. No server, no credit card.

**How it works:** picks an unused, well-documented case from Wikipedia → writes a factual
script (Google Gemini, or Cloudflare as a backup) → makes one simple, minimalist comic-style picture per scene
(Cloudflare Workers AI) → narrates it (Google's Gemini voices, free tier) → adds captions and
slow zoom/pan motion (ffmpeg) → puts the video on GitHub Pages → queues it in Buffer, which
posts it to TikTok.

## Setup (all doable on a phone)

### 1. Free keys
- **Cloudflare** (pictures, required): sign up at dash.cloudflare.com. Then
  *My Profile → API Tokens → Create Token → "Workers AI" template → Continue → Create Token*
  and copy the token. Your **Account ID** is on the dashboard home page (account menu → Copy account ID).
- **Gemini** (script writer, recommended): aistudio.google.com → *Get API key → Create API key*.
- **Buffer** (posting): connect your TikTok account in Buffer, then create a key at
  publish.buffer.com/settings/api. In Buffer, make sure TikTok posts publish automatically
  (not as reminders), and set your posting times in the queue.

### 2. Add them to this repo
*Settings → Secrets and variables → Actions → New repository secret*, one at a time:

| Name | Value |
|---|---|
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID |
| `CLOUDFLARE_API_TOKEN` | Cloudflare token |
| `GEMINI_API_KEY` | Gemini key |
| `BUFFER_API_KEY` | Buffer key |

### 3. Turn on GitHub Pages
*Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch: `gh-pages`, `/ (root)` → Save.*

### 4. Test
*Actions → Daily true crime video → Run workflow*. Switch **Post to TikTok** off for a
first test. When it finishes (about 10 minutes), open the run and download **video** at the
bottom to watch it.

After that it runs by itself every day: the video is made around 3pm New York time (with
backup tries at 4pm and 5pm in case GitHub's timer is late) and Buffer posts it at 7pm.
Set the posting time in Buffer (one slot per day); change the making time in
`.github/workflows/daily-video.yml`.

**Make a real video right now** (it goes to your Buffer queue): edit `post-now.txt`
(pencil icon, change anything, Commit). **Make a test video** (Buffer draft only): edit `test-run.txt`.

**When the free online services run out**, the video is still made: pictures come from a
comic-style generator (DreamShaper + LCM, CreativeML OpenRAIL-M) and narration from the Kokoro voice
model (Apache 2.0), both running on GitHub's machine. Slower and a bit simpler, but no daily limit.

## Changing things
Edit `config.json` (pencil icon on GitHub):
- `category_groups`: Wikipedia categories to pick cases from, by region (each region equally likely).
  Only murder cases in the United States or Europe are used (`murder_cases_only`, `us_and_europe_only`)
- `gemini_voice`: narrator voice. Good serious male voices: `Charon`, `Algenib` (gravelly), `Orus`,
  `Iapetus`, `Gacrux`; female: `Kore`, `Schedar`, `Achernar`. `gemini_voice_style` sets the delivery
- `words_min` / `words_max`: video length (about 150 words ≈ 1 minute)
- `cloudflare_voice`: narrator voice, e.g. `zeus`, `orion`, `arcas`, `apollo` (male) or `luna`, `athena`, `hera` (female)
- `buffer_mode`: `addToQueue` (next queue slot) or `shareNow`
- `image_style`: the art style. Default is color comic book; for manga, copy the text of
  `image_style_manga_example` into `image_style`
- `sfx`: comic sound-effect words on screen (off: `false`)
- `text_retries`: pictures are checked for lettering and remade up to this many times
- `music` / `music_volume`: low, ominous background music, made fresh for every video by
  `music.py` (original, so nothing to license); it dips automatically while the narrator speaks

The script writer defines the key people in each story once (look, age, era clothing) and
every picture reuses that description, so the same characters appear throughout. It also records
where and when the case happened, and every picture is drawn in that country and era (local
architecture, transport, police uniforms, courtrooms; horse-drawn carriages before about 1900),
with at most one person per picture. Any lettering the check still finds is blurred out.

`state/history.json` lists every video made; `state/used.json` stops cases repeating.

## Please keep in mind
These are real people. The script writer is told to stick to the article's facts, use
"alleged" for anyone not convicted, and avoid gore, but AI can still get things wrong.
Check your Buffer queue now and then, and delete anything that looks off. TikTok requires
AI-generated content to be labeled; the workflow asks Buffer to set that label.
Wikipedia text is CC BY-SA, so every caption credits the article.
