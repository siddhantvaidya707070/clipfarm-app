# Clipfarm

Self-hosted video clipping app: customized SupoClip frontend and backend, clip workers, live capture, supervisor, PostgreSQL and Redis. The complete customized SupoClip source is included in `vendor/supoclip`; no upstream download is needed for the main app. Upstream attribution and license are preserved there. `engines.lock.json` records the source snapshot origin; this repository includes local modifications on top of it.

## Fresh installation

Requirements: Git, Python 3.11 or newer, and Docker Engine with Docker Compose v2 (Docker Desktop with Linux containers on Windows). Allow ample disk for image builds, model downloads and your own media. The initial build can take a while. Ports 3107, 8787 and 8788 must be available.

```sh
git clone https://github.com/siddhantvaidya707070/clipfarm-app.git
cd clipfarm-app
python scripts/bootstrap.py
```

Bootstrap generates unique database passwords, internal tokens, local folders and database initialization SQL. Do not copy `.env.example` over the generated `.env`; its `generated-by-bootstrap` values are placeholders. Edit the generated `.env` and set `OPENAI_API_KEY` to your own key. Text analysis, judging and transcription model names are configurable there; use models available to your provider account. Paid API use is charged to your account. Review the configured budgets before processing media.

Also save the same OpenAI key, as plain text with no quotes, in `secrets/openai_key` for supervisor features that read the local secret file. This file and `.env` are ignored by Git. YouTube and Telegram are optional; leave those values empty until needed.

```sh
docker compose build
docker compose up -d --wait
python scripts/create_owner.py
```

The owner setup asks for a password interactively, applies the included additive frontend schema compatibility SQL, and enables the fresh database's `owner@clipfarm.local` account. Sign in at [http://localhost:3107](http://localhost:3107) with that email and your chosen password. Signup is disabled by default. The helper is for first installation; it preserves an existing credential rather than resetting its password. Keep credentials locally in a password manager.

The supervisor UI is at [http://localhost:8787](http://localhost:8787); backend API docs are at [http://localhost:8788/docs](http://localhost:8788/docs). The app binds to localhost. For a remote host, domain, HTTPS and OAuth callback configuration require additional setup.

Use the root `compose.yaml`, not the upstream example compose file under `vendor/supoclip`. The optional OpenShorts and Medusa candidates are not required or enabled. The optional legacy scheduler/n8n profile is also off by default.

## Optional connections

- YouTube publishing: create your own Google OAuth client and enable the YouTube Data API and Analytics API. Set `YOUTUBE_OAUTH_CLIENT_ID` and `YOUTUBE_OAUTH_CLIENT_SECRET` in `.env`; register `http://localhost:3107/api/social/callback/youtube` as a redirect URI. Recreate affected services after configuration, then connect your own YouTube channel through the app. Existing accounts and OAuth tokens are never included.
- Legacy supervisor YouTube: use your own `secrets/google_client.json` and register `http://localhost:8787/oauth/callback` if using that separate connection flow.
- Telegram: configure your own bot token through supervisor controls and your own `TELEGRAM_OWNER_ID`.
- Inputs: provide your own authorized video files or supported video/livestream URLs. This repository contains no user's videos, processed clips, database, model cache or browser authentication state.

## Operation

```sh
docker compose ps
docker compose logs --tail 50 backend worker supervisor
docker compose stop
docker compose up -d
```

PostgreSQL and Redis persist in named Docker volumes. Media, initialization SQL and caches live under ignored `data/`; credentials live under ignored `secrets/` and `.env`. Back these up privately. `docker compose down -v` deletes named volumes. Do not run owner/bootstrap setup against someone else's existing database or reuse their credentials.

## Development and checks

```sh
python -m venv .venv
# Activate .venv using your shell, then:
python -m pip install -r requirements.txt
python -m pytest tests -q
```

Docker installs FFmpeg and the browser runtime. Frontend and backend dependency lockfiles are included with the customized source. Optional browser verification scripts require their own test inputs and local accounts; do not run publication scripts casually against real channels. A fresh full-stack build requires the dependencies and image registries to be reachable. Repository validation does not establish every provider integration or real-stream workflow.

## Public documentation

The existing root homepage, privacy policy and terms remain in place for GitHub Pages: [homepage](https://siddhantvaidya707070.github.io/clipfarm-app/), [privacy](https://siddhantvaidya707070.github.io/clipfarm-app/privacy.html), [terms](https://siddhantvaidya707070.github.io/clipfarm-app/terms.html). `public-site/` contains the site source copy.
