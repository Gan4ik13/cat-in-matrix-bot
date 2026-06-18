# AGENTS.md

## Workspace Structure

Two separate bot projects in one repo:

- **`telegram_autocontent/`** — Full-featured Python auto-posting bot (RSS/Reddit → LLM → Telegram). Production-ready architecture.
- **`bot.py` + `config.yaml` (root)** — Single-file simpler version, same concept, JSON queue. Quick prototyping / fallback.
- **`pulse/`** — Node.js monorepo (npm workspaces), Telegram Mini App "Reaction Duels". Separate project, not a bot.

## Telegram AutoContent Bot (`telegram_autocontent/`)

### Architecture

```
Sources (RSS, Reddit, NewsAPI)
  → Pipeline.run_sourcing()  — collects raw items, LLM rewrites into posts
  → SQLite queue (data/bot.db) — deduplication via SHA1 signature
  → Pipeline.run_publishing() — pops oldest pending, sends to Telegram channel
```

Key files:
- `main.py` — entrypoint, APScheduler-based daemon
- `src/pipeline.py` — orchestrator connecting all components
- `src/generator.py` — LLMGenerator (OpenAI-compatible API) + TemplateGenerator fallback
- `src/storage.py` — SQLite with thread-safe access, dedup by normalized text hash
- `src/publisher.py` — Telegram Bot API sendMessage (with markdown→plain-text fallback)
- `src/sources/` — RSSSource, RedditSource, NewsAPISource (all implement `ContentSource` protocol)
- `config.yaml` — all settings, supports `${ENV_VAR}` substitution

### Run Commands

```bash
# From telegram_autocontent/ directory:
pip install -r requirements.txt

python main.py                    # daemon (sourcing + publishing on cron)
python main.py --once source      # one sourcing cycle, then exit
python main.py --once publish     # one publish, then exit
python main.py --status           # show queue stats
```

### Config

`config.yaml` has live bot token committed — rotate if public. Key sections:
- `telegram` — bot_token, channel (@username)
- `niche` — topic, prompt template for LLM
- `sources` — RSS feeds (enabled), Reddit (enabled), NewsAPI (disabled, no key)
- `generator` — OpenAI-compatible endpoint (currently Ollama localhost:11434)
- `schedule` — cron expressions for sourcing and publishing
- `dedup` — lookback window and similarity threshold
- `storage` — SQLite path

Environment variables: `TG_BOT_TOKEN` overrides config via `${TG_BOT_TOKEN}` placeholder.

### Dependencies

Python 3.10+, key packages: `requests`, `pyyaml`, `feedparser`, `APScheduler`, `openai`.

## Root Bot (`bot.py`)

Single-file alternative. Simpler queue (JSON file in `%LOCALAPPDATA%/telegram_autocontent/`). Uses raw `requests.post` to Ollama `/api/generate` (not OpenAI-compatible). Falls back to hardcoded cat facts templates.

```bash
python bot.py generate    # generate 3 posts to queue
python bot.py post        # publish one now
python bot.py status      # show queue state
python bot.py             # auto-posting loop
```

## Conventions

- Python code: UTF-8, Russian comments/docstrings, no type checker configured
- Config: YAML with `${VAR}` env substitution pattern
- All source classes follow `ContentSource` protocol with `fetch() -> list[RawItem]`
- Generator has `Protocol` interface: `rewrite(item)` and `generate_idea(topic)`
- SQLite storage is thread-safe (APScheduler runs jobs in threads)
- Publisher retries as plain text if Markdown parse fails
- No test suite present
- No CI/CD configured
