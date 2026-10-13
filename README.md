# True Crime TikTok

Every day this repo makes a narrated true-crime video of over a minute from a Wikipedia
article and sends it to TikTok through Buffer. Everything runs on GitHub's free machines
and free services. No server, no credit card.

**How it works:** picks an unused, well-documented case from Wikipedia → writes a factual
script (Google Gemini, or Cloudflare as a backup) → makes one AI picture per scene
(Cloudflare Workers AI) → narrates it (Microsoft's free neural voices) → adds captions and
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

After that it runs by itself every day. Change the time in `.github/workflows/daily-video.yml`.

## Changing things
Edit `config.json` (pencil icon on GitHub):
- `categories`: Wikipedia categories to pick cases from
- `words_min` / `words_max`: video length (about 150 words ≈ 1 minute)
- `voice`: e.g. `en-US-ChristopherNeural`, `en-US-GuyNeural`, `en-US-AndrewNeural`, `en-US-AriaNeural`
- `buffer_mode`: `addToQueue` (next queue slot) or `shareNow`

`state/history.json` lists every video made; `state/used.json` stops cases repeating.

## Please keep in mind
These are real people. The script writer is told to stick to the article's facts, use
"alleged" for anyone not convicted, and avoid gore, but AI can still get things wrong.
Check your Buffer queue now and then, and delete anything that looks off. TikTok requires
AI-generated content to be labeled; the workflow asks Buffer to set that label.
Wikipedia text is CC BY-SA, so every caption credits the article.
