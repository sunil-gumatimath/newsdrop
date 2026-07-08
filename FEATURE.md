# Newsdrop Bot Features & Commands

This document contains a comprehensive list of all the features and commands implemented in the `newsdrop` Telegram bot.

## Commands List

| Command | Function Link | Explanation |
|---|---|---|
| `/start` | [start](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/src/newsdrop/bot/commands.py#L53) | Starts the bot and displays a welcome message with instructions. |
| `/news` | [news](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/src/newsdrop/bot/commands.py#L70) | Fetches and sends a single, compact HTML digest of top headlines based on your preferences. |
| `/search <topic>` | [search](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/src/newsdrop/bot/commands.py#L126) | Searches for news on a specific topic with rate-limiting cooldown protection. |
| `/follow <topic>` | [follow_topic](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/src/newsdrop/bot/commands.py#L177) | Follows a custom topic (e.g. AI, crypto) to include in your personalized news tracking. |
| `/unfollow <topic>` | [unfollow_topic](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/src/newsdrop/bot/commands.py#L199) | Stops following a previously followed custom topic. |
| `/follows` (alias `/topics`) | [list_followed_topics](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/src/newsdrop/bot/commands.py#L221) | Displays a list of all custom topics you are currently following. |
| `/unfollowall` | [unfollow_all_topics](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/src/newsdrop/bot/commands.py#L236) | Removes all custom topics you follow, requiring an inline confirmation first. |
| `/subscribe` | [subscribe](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/src/newsdrop/bot/commands.py#L307) | Enables daily automated news briefs delivered at your scheduled time. |
| `/unsubscribe` | [unsubscribe](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/src/newsdrop/bot/commands.py#L326) | Disables the daily automated news brief delivery. |
| `/setcountry` | set_country | Region picker (includes **World / International** + 10 countries). |
| `/setcategory` | set_category | Preferred news category (7 options) via inline buttons. |
| `/settime` | set_time | Preferred local hour for the daily digest. |
| `/settimezone` | set_timezone | IANA timezone for digests and quiet hours. |
| `/quiet` | quiet_hours | Quiet hours for breaking alerts (`/quiet 22 7` or `/quiet off`). |
| `/prefs` | preferences | Region, category, schedule, quiet hours, breaking settings. |
| `/breaking` | breaking_toggle | Toggle alerts; optional “followed topics as alerts”. |
| `/breakkeywords` | breakkeywords | Personal alert keywords (add/remove/clear). |
| `/trending [category]` | trending | Trending topics, optionally by category. |
| `/health` | health | **Admin only** (`ADMIN_CHAT_IDS`); ops diagnostics. Prefer HTTP `/health`. |
| `/clear` | clear_chat | Tries to delete recent **bot-accessible** messages (not full chat history). |
| `/help` (alias `/commands`) | help_command | Grouped command list (Daily / Discover / Alerts / Utilities). |

## Core Integration Architecture

For detailed information on the multi-source aggregation (API and RSS feeds), database design (SQLite with WAL), and deployment configurations, refer to the [README.md](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/README.md).
