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
| `/setcountry` | [set_country](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/src/newsdrop/bot/commands.py#L261) | Allows you to choose your news region (supporting 10 countries) via inline buttons. |
| `/setcategory` | [set_category](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/src/newsdrop/bot/commands.py#L284) | Allows you to select your preferred news category (7 options) via inline buttons. |
| `/prefs` | [preferences](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/src/newsdrop/bot/commands.py#L340) | Displays your saved settings (region, category, subscription, and breaking news alerts). |
| `/breaking` | [breaking_toggle](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/src/newsdrop/bot/commands.py#L425) | Toggles the scheduled breaking news alerts check (every 30 minutes) on or off. |
| `/trending [category]` | [trending](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/src/newsdrop/bot/commands.py#L453) | Displays globally trending topics, optionally filtered by a specific category. |
| `/health` | [health](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/src/newsdrop/bot/commands.py#L479) | Checks and reports on database health, API rate limit usage, and connectivity status. |
| `/clear` | [clear_chat](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/src/newsdrop/bot/commands.py#L532) | Cleans up and deletes the last ~60 messages in the chat history after user confirmation. |
| `/help` (alias `/commands`) | [help_command](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/src/newsdrop/bot/commands.py#L368) | Displays the list of all available commands and how to use them. |

## Core Integration Architecture

For detailed information on the multi-source aggregation (API and RSS feeds), database design (SQLite with WAL), and deployment configurations, refer to the [README.md](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/README.md).
