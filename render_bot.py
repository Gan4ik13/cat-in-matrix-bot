#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram AutoContent Bot — Render.com deployment.

Комбинирует лучшее из bot.py и telegram_autocontent/:
- aiohttp веб-сервер (health check + self-ping для Render free tier)
- APScheduler для расписания
- SQLite для дедупликации (никаких повторов)
- OpenRouter бесплатные модели для генерации
- Шаблонный fallback если LLM недоступен
"""

import os
import sys
import json
import time
import random
import re
import hashlib
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp.web
import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")

# ============================================================
#  Конфигурация из env
# ============================================================

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHANNEL = os.environ.get("TG_CHANNEL", "@cat_in_matrixx")
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")
PORT = int(os.environ.get("PORT", 8080))

OPENROUTER_MODELS = [
    "openrouter/free",
    "google/gemma-4-26b-a4b-it:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "liquid/lfm-2.5-1.2b-instruct:free",
]

# ============================================================
#  SQLite: очередь + дедупликация
# ============================================================

DB_PATH = Path(os.environ.get("DATA_DIR", "data")) / "bot.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
_conn.row_factory = sqlite3.Row


def _init_db():
    _conn.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            source        TEXT NOT NULL DEFAULT 'generated',
            body          TEXT NOT NULL,
            image_url     TEXT,
            signature     TEXT,
            status        TEXT NOT NULL DEFAULT 'pending',
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            published_at  TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_posts_sig ON posts(signature);
        CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status, created_at);
        CREATE TABLE IF NOT EXISTS published_hashes (
            hash TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    try:
        _conn.execute("ALTER TABLE posts ADD COLUMN image_url TEXT")
        _conn.commit()
    except Exception:
        pass


def _signature(text: str) -> str:
    norm = "".join(c.lower() for c in text if c.isalnum())[:500]
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def _add_post(body: str, image_url: str | None = None, source: str = "generated") -> bool:
    sig = _signature(body)
    cur = _conn.execute("SELECT 1 FROM posts WHERE signature = ? LIMIT 1", (sig,))
    if cur.fetchone():
        log.warning("Дубликат пропущен: %s...", body[:60])
        return False
    _conn.execute(
        "INSERT INTO posts (body, image_url, signature, status, source) VALUES (?, ?, ?, 'pending', ?)",
        (body, image_url, sig, source),
    )
    _conn.commit()
    return True


def _get_pending() -> tuple | None:
    cur = _conn.execute(
        "SELECT id, body, image_url FROM posts WHERE status='pending' ORDER BY created_at LIMIT 1"
    )
    row = cur.fetchone()
    return (row["id"], row["body"], row["image_url"]) if row else None


def _mark_published(post_id: int):
    _conn.execute(
        "UPDATE posts SET status='published', published_at=? WHERE id=?",
        (datetime.utcnow().isoformat(), post_id),
    )
    _conn.commit()


def _mark_failed(post_id: int):
    _conn.execute("UPDATE posts SET status='failed' WHERE id=?", (post_id,))
    _conn.commit()


def _pending_count() -> int:
    return _conn.execute("SELECT COUNT(*) FROM posts WHERE status='pending'").fetchone()[0]


def _recent_hashes(days: int = 14) -> set[str]:
    cur = _conn.execute(
        "SELECT hash FROM published_hashes WHERE created_at >= ?",
        ((datetime.utcnow() - timedelta(days=days)).isoformat(),),
    )
    return {row[0] for row in cur.fetchall()}


def _save_published_hash(text: str):
    h = _signature(text)
    _conn.execute("INSERT OR IGNORE INTO published_hashes (hash) VALUES (?)", (h,))
    _conn.commit()


# ============================================================
#  Генерация постов
# ============================================================

TOPIC = "Коты, мемы, технологии и забавные истории из жизни котов-программистов"
SYSTEM_PROMPT = """Ты — редактор Telegram-канала «Кот в матрице».

ПРАВИЛА (строго):
- Ответить ТОЛЬКО одним готовым постом. Никаких рассуждений, планов, подсчётов символов.
- Запрещено писать "We need", "Let's", "I'll", "Here's", "count" и т.п.
- Никогда не показывай процесс создания поста.
- Объём: 50–200 символов. Коротко и смешно.
- Начинай с эмодзи из списка: 🐱 😼 😺 😸 🐾 🤖 ⚡ 🧠 💻 🔧 🖥️ 📱 💾 🔌 📡
- Без хэштегов, без ссылок.
- Только один пост, без объяснений, без заголовков.

Примеры:
🐱 Кошки не ловят мышей. Они проводят тренировки.
🤖 Мой кот сидит на клавиатуре. Git blame показывает на него.
🧠 Если кот смотрит в пустоту — значит он обновляет прошивку.
⚡ Wi-Fi работает медленнее, когда кот лежит на роутере. Это не баг, это фича."""

CAT_FACTS = [
    "Кошки тратят 70% жизни на сон, а оставшиеся 30% — на то, чтобы судить тебя.",
    "Если кот смотрит в пустоту, значит он обновляет прошивку.",
    "Кот может запрыгнуть на высоту в 5 раз больше своего роста. Просто не хочет.",
    "Wi-Fi работает медленнее, когда кот лежит на роутере. Это не баг, это фича.",
    "Мурлыканье кошки лечит. Но только если она сама этого хочет.",
    "Кот знает, где ты прячешь колбасу. Он просто ждёт подходящего момента.",
    "Если кот принёс тебе мышь — он считает, что ты бесполезный охотник.",
    "Кошачий глаз отражает свет, потому что внутри установлена лазерная указка.",
    "Кот не игнорирует тебя. Он просто на паузе.",
    "Код, написанный при мурлыкании кота, работает быстрее. Это научно.",
    "Кот сидит в коробке не потому что влез. Он там — потому что коробка.",
    "Если кот мяукает в 4 утра — значит, пора вставать. Или кормить.",
    "Девять жизней — это не привилегия. Это бэкап.",
    "Коты не ломают вещи. Они тестируют прочность.",
    "Если робот-пылесос посреди ночи включился — это не баг. Это кот нажал.",
    "Кот программист: сел на клавиатуру, получил доступ ко всем системам.",
    "404 Not Found — когда кот спрятал файл.",
    "Коты изобрели облако задолго до Amazon.",
    "Кот сидит на ноутбуке не ради работы. Он тестирует thermal throttling.",
    "Git commit с котом на клавиатуре — лучший коммит.",
    "Ctrl+Z не работает, когда кот смахнул кружку с клавиатуры.",
    "Кот-тестировщик: нашёл баг, сел на клавиатуру, закрыл тикет.",
    "Если кот лежит на документе — значит, код одобрен.",
    "Кот не лагает. Он буферизуется.",
    "Exception handling: кот поймал мышь и выбросил исключение.",
    "Кот-девопс: поднял кластер из коробок и запустил мониторинг.",
    "Pull request от кота: +1000 строк мурлыкания, 0 конфликтов.",
    "Кот не спамит. Он асинхронно рассылает приветствия.",
    "Machine learning по котам: 100% accuracy — кот всегда прав.",
    "Кот-продакт: требует фичу «корм в 3 утра» уже который год.",
    "Кот сидит на клавиатуре — это не баг, это code review.",
    "Если кот мурлычет — значит, деплой прошёл успешно.",
    "Кот не забивает память. Он кэширует.",
    "Stack overflow: когда кот сбросил все книги с полки.",
    "Кот-аналитик: три часа смотрел на графики, потом уснул.",
    "Если кот шипит — значит, получил segmentation fault.",
    "Кот не дебажит. Он ведёт расследование.",
    "Kubernetes: кот запустил 12 контейнеров из коробок.",
    "Кот не спамит кнопку. Он делает fuzz-тестирование.",
    "Docker: кот упаковал себя в коробку и стал контейнером.",
    "Если кот хвостом виляет — значит, деплой прошёл.",
    "Кот-сьоресер: знает все пароли, но молчит.",
    "CI/CD: кот автоматизировал кормление.",
    "Redis: кот кэширует лежбища в оперативке.",
    "Кот не игнорирует таск-трекер. Он в agile-режиме.",
    "Если кот смотрит в экран — значит, ревьюит код.",
    "Kafka: кот обрабатывает очередь из мурлыканий.",
    "Кот не деплоит в пятницу. Он деплоит в 3 часа утра.",
    "GraphQL: кот запрашивает только нужное количество корма.",
    "Если кот свернулся в клубок — значит, компилируется.",
]

CLOSINGS = [
    "Кот из матрицы одобряет.",
    "P.S. Не забудь погладить кота.",
    "Мурррр.",
    "Шерсть повсюду — это любовь.",
    "Бип-буп-мяу.",
    "Код компилируется, кот мурлычет.",
    "Ctrl+Z в реальной жизни не работает.",
    "Коммит в main? Без страха.",
    "Лапки на клавиатуре — лучший code review.",
    "Если бы кошки управляли серверами, простоев бы не было.",
]


_model_index = 0


def _next_model() -> str:
    global _model_index
    model = OPENROUTER_MODELS[_model_index % len(OPENROUTER_MODELS)]
    return model


EMOJIS = ["🐱", "😼", "😺", "😸", "🐾", "🤖", "⚡", "🧠", "💻", "🔧", "🖥️", "📱", "💾", "🔌", "📡"]


def _is_valid_post(text: str) -> bool:
    if not text or len(text) < 10 or len(text) > 300:
        return False
    first_char = text.lstrip()[0] if text.lstrip() else ""
    if first_char not in EMOJIS:
        return False
    english_words = re.findall(r'[a-zA-Z]{4,}', text)
    if len(english_words) > 3:
        return False
    trash_patterns = [
        "we need", "let's", "i'll", "we'll", "count", "string:",
        "should be", "approximately", "characters", "hashtag",
        "let me", "craft", "produce", "analysis", "short joke",
        "start with", "must be", "no hashtags", "no analysis",
        "something like", "write something", "ensure",
    ]
    text_lower = text.lower()
    if sum(1 for p in trash_patterns if p in text_lower) >= 2:
        return False
    return True


META_STARTS = [
    "we need", "we'll", "let's", "i'll", "i will", "here's", "here is",
    "the post", "my post", "response:", "output:", "result:",
    "craft", "produce", "generate", "create", "write",
    "short joke", "short post", "observation", "meme caption",
    "example", "sure", "okay", "ok,",
]


def _strip_thinking(text: str) -> str | None:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL)
    text = re.sub(r"<scratchpad>.*?</scratchpad>", "", text, flags=re.DOTALL)
    text = re.sub(r"<\|think\|>.*?<\|think\|>", "", text, flags=re.DOTALL)

    text = text.strip()
    if "\n\n" in text:
        text = text.split("\n\n")[0]

    text = text.strip().strip('"').strip("'").strip()

    for line in text.split("\n"):
        line = line.strip().strip('"').strip("'").strip()
        if not line:
            continue
        line_lower = line.lower()
        if any(line_lower.startswith(p) for p in META_STARTS):
            continue
        if _is_valid_post(line):
            return line

    if _is_valid_post(text):
        return text

    return None


def _get_cat_image_url() -> str | None:
    try:
        resp = requests.get("https://api.thecatapi.com/v1/images/search", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data and len(data) > 0:
            return data[0].get("url")
    except Exception as e:
        log.warning("Cat API error: %s", e)
    try:
        resp = requests.get("https://cataas.com/cat", timeout=10, allow_redirects=True)
        if resp.status_code == 200:
            return resp.url
    except Exception as e:
        log.warning("CATAAS error: %s", e)
    return None


POST_PROMPTS = [
    "Придумай короткую шутку (1-2 предложения) про кота и компьютер/интернет/программирование.",
    "Напиши смешное наблюдение про кота, который ведёт себя как программист.",
    "Придумай мем-подпись к фото кота: кот делает что-то странное с техникой.",
    "Напиши короткую шутку про кота, который взломал систему или сел на клавиатуру.",
    "Придумай забавную ситуацию: кот VS робот-пылесос / умный дом / WiFi.",
    "Напиши шутку про кота-хакера или кота-айтишника.",
    "Придумай короткую поговорку про котов и технологии.",
    "Напиши смешной твит от имени кота-программиста (1-2 предложения).",
]


def _generate_llm() -> str | None:
    if not OPENROUTER_KEY:
        return None
    tried = set()
    user_prompt = random.choice(POST_PROMPTS)
    for attempt in range(len(OPENROUTER_MODELS)):
        model = _next_model()
        if model in tried:
            global _model_index
            _model_index += 1
            if len(tried) >= len(OPENROUTER_MODELS):
                break
            continue
        tried.add(model)
        try:
            log.info("LLM: пробуем модель %s...", model)
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 1.0,
                    "max_tokens": 100,
                },
                timeout=60,
            )
            if resp.status_code == 429:
                wait = 10 * (attempt + 1)
                log.warning("Rate limit (429) на %s, пробуем следующую модель...", model)
                _model_index += 1
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()
            cleaned = _strip_thinking(text)
            if cleaned:
                log.info("LLM: успешно с моделью %s: %s", model, cleaned[:60])
                return cleaned
            log.warning("LLM: ответ от %s не прошёл валидацию: %s...", model, text[:80])
        except Exception as e:
            log.warning("LLM генерация не удалась с %s (попытка %d): %s", model, attempt + 1, e)
            _model_index += 1
            time.sleep(3)
    return None


_used_facts: set[str] = set()


def _get_used_facts() -> set[str]:
    try:
        cur = _conn.execute(
            "SELECT DISTINCT body FROM posts WHERE status IN ('pending', 'published') AND source = 'template' ORDER BY created_at DESC LIMIT 100"
        )
        return {row[0] for row in cur.fetchall()}
    except Exception:
        return set()


def _generate_template() -> str:
    global _used_facts
    if not _used_facts:
        _used_facts = _get_used_facts()

    available = [f for f in CAT_FACTS if f not in _used_facts]
    if not available:
        _used_facts.clear()
        available = CAT_FACTS

    fact = random.choice(available)
    emoji = random.choice(EMOJIS)
    _used_facts.add(fact)
    return f"{emoji} {fact}"


def _generate_one() -> tuple[str, str | None]:
    post = _generate_llm()
    if not post:
        post = _generate_template()
    image_url = _get_cat_image_url()
    return post, image_url


# ============================================================
#  Публикация в Telegram
# ============================================================

def _send_to_tg(text: str, image_url: str | None = None) -> bool:
    if image_url:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": TG_CHANNEL,
            "photo": image_url,
            "caption": text,
        }
    else:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TG_CHANNEL,
            "text": text,
            "disable_web_page_preview": True,
        }
    try:
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok"):
            return True
        log.error("Telegram API: %s", data)
    except Exception as e:
        log.error("Ошибка отправки: %s", e)
    return False


# ============================================================
#  APScheduler jobs
# ============================================================

MIN_QUEUE = 3


def job_sourcing():
    log.info("=== SOURCING: проверяем очередь (%d в очереди) ===", _pending_count())
    if _pending_count() >= MIN_QUEUE:
        log.info("Очередь достаточно полная, пропускаем sourcing")
        return

    target = 5
    added = 0
    for i in range(target):
        post, image_url = _generate_one()
        source = "template" if post and any(f in post for f in CAT_FACTS) else "generated"
        if _add_post(post, image_url, source):
            added += 1
        time.sleep(2)

    if added == 0:
        log.warning("LLM не сработал, добавляем шаблонные посты")
        for i in range(3):
            post = _generate_template()
            image_url = _get_cat_image_url()
            if _add_post(post, image_url, "template"):
                added += 1

    log.info("=== SOURCING завершён: добавлено %d постов (всего: %d) ===",
             added, _pending_count())


def job_publishing():
    log.info("=== PUBLISHING ===")
    result = _get_pending()
    if not result:
        log.info("Очередь пуста, запускаем sourcing...")
        job_sourcing()
        result = _get_pending()
        if not result:
            log.warning("Всё равно пусто, пропускаем")
            return

    post_id, body, image_url = result
    log.info("Публикуем: %s...", body[:80])

    if _send_to_tg(body, image_url):
        _mark_published(post_id)
        _save_published_hash(body)
        log.info("Опубликовано! В очереди: %d", _pending_count())
    else:
        _mark_failed(post_id)
        log.error("Не удалось опубликовать, помечено failed")


# ============================================================
#  aiohttp: health check + self-ping
# ============================================================

async def handle_health(request):
    return aiohttp.web.json_response({
        "status": "ok",
        "pending": _pending_count(),
        "uptime": str(datetime.utcnow() - start_time),
    })


async def handle_root(request):
    return aiohttp.web.json_response({"status": "ok", "bot": "cat-in-matrix"})


async def self_ping():
    """Self-ping каждые 10 минут чтобы Render free не заснул."""
    while True:
        await asyncio.sleep(600)
        try:
            port = PORT
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://localhost:{port}/health") as resp:
                    log.info("Self-ping: %s", resp.status)
        except Exception as e:
            log.warning("Self-ping failed: %s", e)


start_time = datetime.now()


# ============================================================
#  Main
# ============================================================

def main():
    if not TG_BOT_TOKEN:
        log.error("TG_BOT_TOKEN не задан!")
        sys.exit(1)

    _init_db()
    log.info("Бот запущен. Канал: %s", TG_CHANNEL)
    log.info("LLM: %s", "включён" if OPENROUTER_KEY else "выключен (шаблоны)")
    log.info("Очередь: %d постов", _pending_count())

    # aiohttp + scheduler — всё внутри event loop
    app = aiohttp.web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)

    runner = aiohttp.web.AppRunner(app)

    async def start():
        # HTTP server
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        log.info("HTTP сервер на порту %d", PORT)

        # APScheduler — start inside running event loop
        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.add_job(
            job_sourcing,
            CronTrigger.from_crontab("0 */2 * * *"),
            id="sourcing",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            job_publishing,
            CronTrigger.from_crontab("*/10 * * * *"),
            id="publishing",
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        log.info("Планировщик запущен")

        # Self-ping в фоне
        asyncio.ensure_future(self_ping())

        # Стартовый sourcing + публикация в фоне
        loop = asyncio.get_event_loop()

        def _initial_run():
            if _pending_count() < MIN_QUEUE:
                log.info("Стартовое наполнение очереди...")
                job_sourcing()
            if _pending_count() > 0:
                log.info("Стартовая публикация...")
                job_publishing()

        loop.run_in_executor(None, _initial_run)

        # Бесконечный цикл
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            scheduler.shutdown()
            await runner.cleanup()

    try:
        asyncio.run(start())
    except KeyboardInterrupt:
        log.info("Остановлен пользователем")


if __name__ == "__main__":
    main()
