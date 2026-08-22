# Newsdrop Bot Features & Commands

Comprehensive list of user-facing features and slash commands in the `newsdrop` Telegram bot.

## Commands

| Command | Handler | Description |
|---|---|---|
| `/start` | `start` | Guided onboarding: **region → category → subscribe / get news / skip**. |
| `/news` | `news` | On-demand HTML digest: blurbs, source/time, why-tags, open-article buttons. Followed topics float first. |
| `/search <topic>` | `search` | Whole-word search with `/news`-style cards, open buttons, and **Follow #topic**. |
| `/follow <topic>` | `follow_topic` | Follow a custom topic (e.g. AI, crypto) for ranking + digest highlights. |
| `/unfollow <topic>` | `unfollow_topic` | Stop following one topic. |
| `/follows` / `/topics` | `list_followed_topics` | List followed topics. |
| `/unfollowall` | `unfollow_all_topics` | Clear all follows (inline confirmation). |
| `/subscribe` | `subscribe` | Enable daily digests at the user’s local hour. |
| `/unsubscribe` | `unsubscribe` | Disable daily digests. |
| `/setcountry` | `set_country` | Region picker (World + 10 countries) via inline buttons. |
| `/setcategory` | `set_category` | Category picker (7 options). |
| `/settime` | `set_time` | Preferred local hour for daily digest. |
| `/settimezone` | `set_timezone` | IANA timezone for digests and quiet hours. |
| `/quiet` | `quiet_hours` | Quiet hours for breaking alerts (`/quiet 22 7` or `/quiet off`). |
| `/prefs` | `preferences` | Show region, category, schedule, quiet hours, breaking settings. |
| `/breaking` | `breaking_toggle` | Toggle alerts; option to use followed topics as keywords. |
| `/breakkeywords` | `breakkeywords` | Add / remove / clear personal alert keywords. |
| `/trending [category]` | `trending` | Trending title keywords; optional category; follow/search buttons. |
| `/clear` | `clear_chat` | Confirm, then delete recent messages the bot is allowed to remove (~150 ID window; private chat ≈ bot messages &lt;48h). |
| `/health` | `health` | **Admin only** (`ADMIN_CHAT_IDS`). Ops diagnostics; prefer HTTP `/health` for probes. |
| `/help` / `/commands` | `help_command` | Grouped command list (Daily / Discover / Alerts / Utilities). |

Bot menu (Telegram command list) includes: start, news, subscribe, search, follow, setcountry, setcategory, settime, settimezone, breaking, prefs, clear, help.

## Product features

### Digests (`/news` + daily job)
- Multi-source cards: linked title, short blurb, relative time, source, multi-outlet “also …”
- Why-tags: `📌 #topic`, `🗞 Multi-source`, `⭐ Trusted`, `🔥 Trending`
- Followed-topic matches sorted to the top
- Inline URL buttons to open full stories
- Empty state with Region / Category / Search shortcuts

### Search
- Whole-word matching (avoids `AI` → `airport` / `against`)
- Relevance ranking (title hits before body)
- Follow-topic button on results

### Breaking alerts
- Single compact message: matched keyword(s), blurb, source, daily cap (`N/max left`)
- **Read full story** button
- Quieter matching: title hit preferred; body-only requires ≥2 keywords
- Quiet hours + per-user keyword lists + optional followed topics
- Dedupe so the same story is not re-sent

### Onboarding
- `/start` walks region → category → subscribe or “Get news now”

### Clear chat
- `/clear` with confirmation; removes bot-accessible recent messages
- Does **not** wipe full Telegram history or all user-typed messages in DMs

### Aggregation & ranking
- NewsData.io + country/category RSS
- Story clustering (near-duplicate titles/URLs)
- Source trust weights (e.g. Reuters, AP, The Guardian)
- Shared API client, cache, and daily request budget

## Regions & categories

**Regions:** World, US, UK, India, Canada, Australia, Germany, France, Japan, Brazil, South Korea  

**Categories:** general, technology, business, sports, entertainment, health, science  

## Related docs

- Architecture & agent notes: [AGENTS.md](./AGENTS.md)
- Setup, env vars, deployment: [README.md](./README.md)
- Security / secrets reporting: [SECURITY.md](./SECURITY.md)
