# One-time setup (~20 minutes, never again after this)

This is the only human work required. Once done, the channel publishes itself
3x/week, forever, until the 15-script queue runs out (~5 weeks) — after that,
add more scripts to `scripts_queue.json` (a 2-minute copy-paste, not production work).

## 1. Create the GitHub repo
- Create a **private** repo (e.g. `revenge-channel-auto`)
- Push this entire folder to it

## 2. ElevenLabs (AI voice) — ~$22/month
- Sign up at elevenlabs.io
- Settings -> API Keys -> copy your key

## 3. Pexels (stock footage) — free
- Sign up at pexels.com/api
- Copy your API key

## 4. YouTube upload access — one-time, Google requires this personally
- Follow the steps inside `get_refresh_token.py` exactly
- It opens a browser once, you approve access to your own channel, done forever

## 5. Add secrets to GitHub
Repo -> Settings -> Secrets and variables -> Actions -> New repository secret.
Add all five:
- `ELEVENLABS_API_KEY`
- `PEXELS_API_KEY`
- `YT_CLIENT_ID`
- `YT_CLIENT_SECRET`
- `YT_REFRESH_TOKEN`

## 6. Done
The workflow in `.github/workflows/publish.yml` runs automatically Mon/Wed/Fri.
No further action from you. Check the channel, not the pipeline.

## What still needs you, honestly
- The 20 minutes above, once
- Adding more scripts to the queue every ~5 weeks (ask for a fresh batch, paste them in)
- Nothing else — no editing, no uploading, no reviewing footage
