# Security Policy

`newsdrop` is a self-hosted Telegram bot. You run it with **your own** API keys on **your own** machine or VPS.

## Supported versions

Security fixes are applied on a best-effort basis to the latest code on `main`. There is no long-term support branch.

## Reporting a vulnerability

**Do not open a public GitHub issue for security-sensitive reports** (especially if they involve leaked tokens, remote code execution paths, or ways to abuse someone else’s running bot).

Please report privately:

1. Use [GitHub Security Advisories](https://github.com/sunil-gumatimath/newsdrop/security/advisories/new) for this repository if available, **or**
2. Contact the maintainer via the email on the GitHub profile / `pyproject.toml` authors field.

Include:

- What you found and how to reproduce it
- Impact (e.g. secret exposure, unauthorized admin access)
- Whether a fix or workaround is already known

You should get an acknowledgement when possible. Timeline depends on severity and maintainer availability (solo project).

## Secrets & configuration

| Secret | Where it belongs |
|--------|------------------|
| `TELEGRAM_BOT_TOKEN` | `.env` only (never commit) |
| `NEWS_API_KEY` | `.env` only (never commit) |
| `REDIS_PASSWORD` | `.env` / host secrets only |

Rules:

- Copy `.env.example` → `.env` and fill real values locally.
- `.env` is gitignored. Do not force-add it.
- Do not paste tokens into issues, PRs, screenshots, or commit messages.
- If a token was ever committed or posted publicly, **revoke and rotate it immediately** (BotFather + NewsData.io dashboard).

## Self-host hardening (recommended)

- Run with Docker Compose as documented; avoid exposing Redis publicly.
- Set `ADMIN_CHAT_IDS` so Telegram `/health` is not world-usable.
- If the bot is shared with many users, set `NEWS_COOLDOWN_SECONDS` and `SEARCH_COOLDOWN_SECONDS` &gt; 0 to protect the NewsData.io free-tier budget.
- Keep dependencies updated (`uv lock` / `pip` upgrades) when practical.
- Restrict who can message the bot if you only use it personally (Telegram privacy / bot settings).

## Out of scope

- Issues that only affect misconfiguration (e.g. publishing your own `.env`)
- Upstream outages (Telegram, NewsData.io, third-party RSS)
- Social-engineering of API providers

## Safe contribution habits

- Never commit real credentials or production database files.
- Prefer fixtures and mocks in tests.
- If a PR needs a secret for CI, use GitHub Actions secrets — not hardcoded values.
