# newsdrop enhancements — plan 2026-09-01 (skip AI)

## Context
Implement all quick/medium wins except AI TL;DR. TDD, batchwise atomic commits.

## Tasks

### T1 Bookmarks
- DB: `saved_articles(chat_id, url, title, saved_at PRIMARY KEY(chat_id,url))` + index saved_at + migration
- DB funcs: save_article, unsave_article, list_saved, is_saved
- Commands: /save <url|reply>, /saved, /unsave <url|index>, /clearbookmarks
- Helpers: inline "Save" button on digest/search cards, callback `save:<hash>`
- Tests: unit test_database + test_commands for save flow

### T2 Digest frequency
- DB: add `digest_frequency TEXT DEFAULT 'daily'` and `digest_days TEXT DEFAULT ''` to user_preferences, migrate
- Config: DAILY_FREQUENCY_CHOICES
- Commands: /setfreq (inline keyboard: daily/twice/weekdays/custom)
- Jobs: send_daily_news checks frequency (twice = 8am+8pm, weekdays = Mon-Fri only)
- Tests: jobs frequency filtering

### T3 Feedback reactions
- DB: `article_feedback(chat_id, article_url, vote INTEGER, created_at)` PK(chat_id, article_url)
- Helpers: 👍 👎 buttons on each card, callback `feedback:<vote>:<hash>`
- Ranker: boost if net positive feedback for source/topic (optional, minimal)
- Tests: feedback storage + toggle

### T4 Export/share
- Command: /export -> generate HTML digest of last briefing (reuse helpers), send as document
- No new DB, pure formatting
- Tests: helper generates valid HTML

### T5 Language filter
- DB: `language TEXT DEFAULT 'en'` in user_preferences
- Config: SUPPORTED_LANGUAGES map
- Commands: /setlang with inline keyboard
- Fetcher: pass `language` to NewsData params if not 'all'
- Tests: payload includes language

### T6 Channel mode
- DB: `channel_id TEXT DEFAULT ''` in user_preferences
- Commands: /setchannel @handle or ID, /setchannel off
- Jobs/Commands: if channel_id set, post digest there (bot must be admin)
- Tests: channel pref storage

## Execution order
T1 -> T2 -> T3 -> T4 -> T5 -> T6 (DB migrations cumulative, avoid conflicts). Each via delegate_task subagent with TDD.
