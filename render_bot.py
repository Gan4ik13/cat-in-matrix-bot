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
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()

OPENROUTER_MODELS = [
    "openrouter/free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "liquid/lfm-2.5-1.2b-instruct:free",
]

# ============================================================
#  SQLite: очередь + дедупликация
# ============================================================

DB_PATH = Path(os.environ.get("DATA_DIR", "data")) / "bot.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=5)
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

TOPIC = "Мемы про котов и IT"

SYSTEM_PROMPT = """Ты — автор мемных подписей для Telegram-канала «Кот в матрице» (коты + IT).

СТИЛЬ: мемная подпись, смешная жизненная ситуация, ирония. Коротко, хлёстко, понятно.
ФОРМАТ: ровно 1 предложение, 40–150 символов. Начни с эмодзи (🐱😼😺😸🐾🤖⚡🧠💻🔧🖥️📱💾🔌📡), пиши на русском.
Запрещено: объяснения, предисловия, хэштеги, ссылки, подсчёт символов, «вот пример», «я написал».

Примеры:
🐱 Когда кот садится на клавиатуру — это не баг, это code review.
🤖 Кот не храпит — он запускает компиляцию.
🧠 Если кот долго смотрит в стену — там обновляется прошивка.
⚡ Кот уснул на сервере — теперь у нас простой по вине кота.
😼 Код с шерстью работает стабильнее — это научный факт.
📡 Кот сидит на роутере — скорость упала, безопасность выросла.
Только один пост. Никаких вариантов."""

CAT_MEMES = [
    "Кот сидит на клавиатуре — не мешай, он пишет код.",
    "Я не сплю, я тестирую закрытые глаза.",
    "Кот не уронил горшок. Он проверил гравитацию.",
    "Если кот смотрит в пустоту — там обновляется прошивка.",
    "Мой план на день: как у кота, но без очарования.",
    "Кот на ноутбуке — это не проблема, это фича терморегуляции.",
    "Зарядка у кота: встал, потянулся, лёг обратно.",
    "Девять жизней — это просто система бэкапов.",
    "Кот не мяукает — он отправляет push-уведомление.",
    "Когда кот принёс мышь — он провёл успешный деплой.",
    "Кот спит на мониторе — греет батарейку.",
    "Кот не врёт. Он просто показывает тебе свою версию правды.",
    "Кот смотрит в монитор — code review идёт полный день.",
    "У кота нет дедлайнов. Только кормёжка.",
    "Кот всё слышит. Особенно открытие пачки корма.",
    "Когда кот трогает лапкой воду — он тестирует API.",
    "Кот не путается под ногами. Он прокладывает маршруты.",
    "Кот сел на клавиатуру — приоритетная задача.",
    "Мурлыканье — это звуковой движок на минималках.",
    "Кот не дерёт диван. Он рефакторит обивку.",
    "Кот не просто спит — он кэширует энергию.",
    "Кот не убегает от пылесоса. Он проводит нагрузочное тестирование.",
    "Кот не царапается. Он аудит безопасности проводит.",
    "Если кот лёг на твой код — значит, он одобрен.",
    "Кот не мешает работать. Он создаёт коллаборативную среду.",
    "Кот не падает — он неудачно синхронизируется.",
    "Кот не тыкается мордой в телефон. Он сканирует QR-код.",
    "Кот не пинает кружку. Он проверяет стол на стрессоустойчивость.",
    "Кот ночью бегает по квартире — запускает ci/cd пайплайн.",
    "Кот в коробке — контейнеризация уровня бог.",
    "Кот не орёт в 5 утра. Это запланированная задача.",
    "Кот не выбрасывает вещи со стола. Он оптимизирует пространство.",
    "Когда кот скидывает телефон — жесткий ресет.",
    "Кот не играет с клубком. Он проводит QA нитки.",
    "Кот сидит на твоей мышке — курсор зафризился.",
    "Кот не лижется — он логирует события дня.",
    "Кот свернулся клубком — компиляция пошла.",
    "Кот не выпрашивает корм — он эскалирует тикет.",
    "Кот не точит когти об диван. Он проводит пентест.",
    "Кот не ленивый. Он ресурсосберегающий.",
    "Кот не уходит, когда его зовёшь — занят в фоновом потоке.",
    "Кот на клавиатуре ввел пароль. Доступ получен.",
    "Система требует перезагрузку — кот нажал красную кнопку.",
    "Кот спит на сервере — даунтайм обеспечен.",
    "Кот скинул вазу — проверка disaster recovery.",
    "Кот прищурился — сканирует уязвимости.",
    "Кот мурчит в трубку — DDoS-атака на поддержку.",
    "Кот не опрокидывает миску — он делает A/B тест голода.",
    "Кот сидит на Wi-Fi роутере — скорость упала вдвое, безопасность выросла.",
    "Кот не просится на ручки — он делает миграцию данных.",
    "Кот не грызёт провод — он тестирует прочность изоляции.",
    "Когда кот наступил на клавиатуру — pull request принят.",
    "Кот не тыкает лапой в экран — он UI тестирует.",
    "Кот не сопит в ухо — он даёт обратную связь в реальном времени.",
    "Кот не орёт — он повышает громкость уведомлений.",
    "Кот уткнулся носом в угол — система зависла.",
    "Кот запутался в проводах — сеть закольцевалась.",
    "Кот сломал цветок — проверка живучести.",
    "Кот не спит на подоконнике — он мониторит периметр.",
    "Кот мяукает закрытой двери — непройденный код-ревью.",
    "Кот разбил кружку — успешное завершение спринта.",
    "Кот скинул носки с батареи — релиз нестабильный.",
    "Кот не прячется — он уходит в офлайн.",
    "Кот не царапает обоpки — он меняет архитектуру.",
    "Кот не мурчит — он запустил фоновый процесс.",
    "Кот не сопит — он парсит сны.",
    "Кот на подоконнике — база данных обновляется.",
    "Кот не орёт еду — он отправляет запрос на сервер.",
    "Кот смотрит телевизор — тестирует видеокарту.",
    "Кот нюхает цветок — сканирует на запахи.",
    "Кот сел на ноутбук — тепловой тест пройден.",
    "Кот не бегает — у него асинхронный режим.",
    "Кот вылизывает лапу — загружает драйверы.",
    "Кот не спит в кровати — он ревьюит архитектуру сна.",
    "Кот залез в шкаф — ищет уязвимости.",
    "Кот не царапает руку — проводит нагрузочный тест.",
    "Кот отводит глаза после разбитой вазы — баг не воспроизводится.",
    "Кот на кухне в 3 ночи — проверка инвентаря.",
    "Кот не ест сухой корм — API не отвечает.",
    "Кот спит в раковине — проверка водонепроницаемости.",
    "Кот орёт без причины — false alarm.",
    "Кот трясёт лапой после воды — отладка мокрого интерфейса.",
    "Кот не встречает с работы — endpoint временно недоступен.",
    "Кот сидит в углу и смотрит в стену —深度 обучение.",
    "Кот на твоём лице утром — brute force авторизация.",
    "Кот не прыгает на холодильник — ждёт апдейт.",
    "Кот не играет с лазером — дебажит точку.",
    "Кот тыкает лапой в миску — голодный запрос в обработке.",
    "Кот сидит на паспорте — документооборот заблокирован.",
    "Кот скинул телефон со стола — crash report отправлен.",
    "Кот грызёт коробку — тестирование прочности упаковки.",
    "Кот уснул на клавиатуре — сессия заблокирована.",
    "Кот застрял в пакете — недокументированная фича.",
    "Кот спит на чистой одежде — QA пройдено.",
    "Кот смотрит в твою тарелку — аудит питания.",
    "Кот прыгнул с полки на полку — load balancer сработал.",
    "Кот трется о ноги — авторизация пройдена.",
    "Кот положил лапу на телефон — вызов отклонён.",
    "Кот скинул пульт — пользовательский интерфейс обновлён.",
    "Кот сидит на книге — чтение заблокировано.",
    "Кот не приходит на имя — переименование не помогло.",
    "Кот смотрит на еду, но не ест — баг с инвентарём.",
    "Кот поцарапал новый диван — приемка не пройдена.",
    "Кот спит мордой в миску — низкий энергопотребляющий режим.",
    "Кот открывает шкаф лапой — навык открытия освоен.",
    "Кот сидит на твоей одежде — приоритет — комфорт кота.",
    "Кот уронил стакан — тест на прочность посуды пройден.",
    "Кот не смотрит в лоток — UX неудовлетворительный.",
    "Кот ловит муху — тест реакции периферии.",
    "Кот топает по клавишам — DDOS на сайт.",
    "Кот сидит на мышке — курсор в режиме ожидания.",
    "Кот закрыл ноутбук — удалённая работа завершена.",
    "Кот положил хвост на экран — тёмная тема активирована.",
    "Кот орëт под дверью ванной — проверка звукоизоляции.",
    "Кот лежит на спине — тест уязвимости.",
    "Кот тычется носом в телефон — Face ID на кота не настроен.",
    "Кот на зарядке телефона — кража энергии.",
    "Кот бежит из другой комнаты на звук корма — быстрый респонс.",
    "Кот спит на посудомойке — вибротест.",
    "Кот на стиральной машине — мониторинг цикла стирки.",
    "Кот на холодильнике — контроль температуры серверной.",
    "Кот в раковине — проверка дренажной системы.",
    "Кот в пустом тазу — тестирование изоляции.",
    "Кот в чемодане — подготовка к миграции.",
    "Кот в сумке — портативная версия.",
    "Кот в коробке из-под обуви — минимальная конфигурация.",
    "Кот в пакете — инкапсуляция.",
    "Кот под одеялом — скрытый режим.",
    "Кот за шторой — стелс-режим активирован.",
    "Кот на клавиатуре ночью — внеплановый деплой.",
    "Кот не пришёл на ужин — обработка исключения.",
    "Кот не отвечает на «кис-кис» — endpoint перегружен.",
    "Кот не выходит из-под дивана — система в офлайне.",
    "Кот свернулся калачиком — режим энергосбережения.",
    "Кот растянулся на весь пол — максимальная площадь захвата.",
    "Кот выгнул спину — defensive programming.",
    "Кот шипит на пылесос — firewall активирован.",
    "Кот не любит новые игрушки — legacy code.",
    "Кот привык к старой миске — обратная совместимость.",
    "Кот не пьёт из новой поилки — баг в интерфейсе.",
    "Кот орёт на пустую миску — system alert.",
    "Кот сидит на кухне у пустой миски — мониторинг ресурсов.",
    "Кот разбудил в 6 утра — непредвиденный сбой.",
    "Кот скинул будильник — сброс настроек.",
    "Кот нажал на кнопку ноутбука — незапланированное завершение.",
    "Кот закрыл крышку ноута — слип режим.",
    "Кот нажал на пробел — пауза в воспроизведении.",
    "Кот нажал Esc — выход без сохранения.",
    "Кот нажал Delete — безвозвратная потеря.",
    "Кот нажал Enter — подтверждение действия.",
    "Кот нажал Ctrl+Alt+Del — перезагрузка системы.",
    "Кот наступил на F5 — обновление страницы.",
    "Кот нажал Alt+F4 — аварийное завершение.",
    "Кот на пробел поставил видео на паузу и ушёл — клиффхэнгер.",
    "Кот нажал Caps Lock — теперь всё КРИЧИТ.",
    "Кот нажал Print Screen — сделал скриншот рабочего стола.",
    "Кот стёр лапой написанный код — git reset --hard.",
    "Кот добавил пробелов в код — линтер недоволен.",
    "Кот прошёлся по клавишам — сгенерировал пароль.",
    "Кот наступил на тачпад — масштабирование изменилось.",
    "Кот крутится на столе — дефрагментация диска.",
    "Кот гоняется за хвостом — бесконечный цикл.",
    "Кот запутался в проводах — спутанная логика.",
    "Кот перегрыз провод — физический уровень повреждён.",
    "Кот трогает лампочку — проверка температуры.",
    "Кот залез в системник — hardware audit.",
    "Кот греет попу о монитор — энергоэффективность.",
    "Кот трется о колонку — тест корпуса на вибрацию.",
    "Кот лежит на роутере — раздача тепла и интернета.",
    "Кот сидит на хабе — центр сети.",
    "Кот на серверной стойке — администратор.",
    "Кот спит на сервере — даунтайм.",
    "Кот не даёт чинить сервер — саботаж.",
    "Кот залез в рюкзак — мобильная версия.",
    "Кот проверяет сумку перед уходом — security check.",
    "Кот забрался на шкаф — мониторинг сверху.",
    "Кот смотрит в окно — внешний аудит.",
    "Кот загорает на подоконнике — зарядка солнечной батареи.",
    "Кот сидит на батарее — подзарядка.",
    "Кот на полу греет лапы — тепловой насос.",
    "Кот в обнимку с батареей — теплообмен.",
    "Кот на полу растянулся — охлаждение системы.",
    "Кот спит в холодильнике — экстремальное охлаждение.",
    "Кот ловит снежинки за окном — тест захвата данных.",
    "Кот смотрит на дождь — мониторинг погоды.",
    "Кот боится грома — триггер ошибки.",
    "Кот не выходит на улицу — закрытая экосистема.",
    "Кот просится на балкон — запрос на расширение прав.",
    "Кот на балконе — на открытом воздухе.",
    "Кот на лестничной клетке — выход в прод.",
    "Кот гуляет сам по себе — децентрализованная система.",
    "Кот пришёл с улицы с мышью — успешный фетч.",
    "Кот принёс птичку — неожиданный результат.",
    "Кот притащил лист — загрузка данных из внешнего источника.",
    "Кот принёс игрушку — запрос на игру принят.",
    "Кот зовёт играть — interactive mode.",
    "Кот тыкает лапой лазер — преследование цели.",
    "Кот играет с бантиком — симуляция охоты.",
    "Кот гоняет шарик по полу — трекинг движения.",
    "Кот играет с коробкой — лучший игровой движок — воображение.",
    "Кот спит в коробке — контейнер остановлен.",
    "Кот сидит в вазе — попытка запихнуть объект в контейнер.",
    "Кот залез в узкую щель — сжатие данных.",
    "Кот застрял в форточке — передача данных с ошибкой.",
    "Кот пролез под диваном — проход по туннелю.",
    "Кот прыгает с дивана на стол — вертикальное масштабирование.",
    "Кот прыгает по полкам — балансировка нагрузки.",
    "Кот упал с полки — rollback.",
    "Кот повис на шторе — незавершённая транзакция.",
    "Кот слетел с подоконника — неожиданный даунтайм.",
    "Кот приземлился на лапы — всегда successful deployment.",
    "Кот упал и делает вид что так и было — фейковый успешный статус.",
    "Кот умывается после падения — скрытие ошибки.",
    "Кот облизывается после еды — лог успешной операции.",
    "Кот сытый и довольный — статус OK.",
    "Кот злой и голодный — критическая ошибка.",
    "Кот не в настроении — настроение: DNF.",
    "Кот грустит — низкий уровень заряда.",
    "Кот не играет — система в режиме ожидания.",
    "Кот задумался — идёт тяжёлый процесс.",
    "Кот медитирует — система в idle.",
    "Кот с закрытыми глазами, но не спит — фоновый процесс запущен.",
    "Кот спит с открытыми глазами — система в гибернации, но следит.",
    "Кот спит и дëргается — снится код.",
    "Кот мяукает во сне — вербальный лог.",
    "Кот бежит во сне — симулятор охоты.",
    "Кот видит сны — тестовая среда.",
    "Кот проснулся и потянулся — перезагрузка завершена.",
    "Кот проснулся и орëт — system wake-up alert.",
    "Кот сразу бежит к миске — автоматический запуск после загрузки.",
    "Кот орëт пока насыпают корм — мониторинг процесса.",
    "Кот не ест новый корм — интерфейс не понравился.",
    "Кот выбирает кусочки из миски — сортировка данных.",
    "Кот не допивает воду — недозагрузка.",
    "Кот пьёт из крана — прямой доступ к источнику.",
    "Кот пьёт из твоего стакана — несанкционированный доступ.",
    "Кот лакает из лужи — использование невалидного источника.",
    "Кот проверил миску и ушёл — false positive.",
    "Кот оставил еду и ушёл — задача прервана.",
    "Кот наелся и мурчит — успешное выполнение.",
    "Кот просит добавки — запрос на дополнительные ресурсы.",
    "Кот украл со стола — воровство данных.",
    "Кот стащил сосиску — несанкционированный доступ к данным.",
    "Кот открыл холодильник — взлом базы данных.",
    "Кот залез в пакет с хлебом — инцидент безопасности.",
    "Кот нюхает еду — проверка подлинности.",
    "Кот не ест без тебя — авторизация обязательна.",
    "Кот ест с руками — прямой доступ без API.",
    "Кот просит корм каждый час — таймер сброшен.",
    "Кот привёл режим кормления к одному — унификация.",
]

MEME_ONELINERS = [
    "Кот. Клавиатура. Баги.",
    "RIP стул. Привет картонка.",
    "КОТ = Космический Оператор Терминала.",
    "Кот не баг, а фича.",
    "Сел на клаву — закоммитил.",
    "404: Корм не найден.",
    "Кот — мой тимлид.",
    "Мурчание — это прод.",
    "Кот продакт: фича \"корм\" в бэклоге 5 лет.",
    "Жизнь — симулятор кота.",
    "Гладить кота — тоже CI/CD.",
    "Кот опять перезагрузил роутер.",
    "Всё, что кот уронил — фича.",
    "Кот не шалит — он тестирует.",
    "Усы, лапы, хвост — вот и весь стек.",
    "Главный баг в системе — кот.",
    "Кот не спит — он компилирует.",
    "Кот сломал прод. Снова.",
    "Эскалация: кот голоден.",
    "Кот подтвердил — gunicorn работает.",
    "Погладь кота — пофикси баг.",
    "Кот в прод не пускает.",
    "Деплой отменён: кот на клавиатуре.",
    "Мониторинг показал: кот спит.",
    "Сервер упал, кот спит.",
    "Алярм: кот открыл холодильник.",
    "Релиз переносится: кот недоволен.",
    "Кот на ревью — багов нет, только шерсть.",
    "Код без мурчания — не код.",
    "Кот — причина всех простоев.",
    "Production ready? Кот проверил.",
    "Hotfix: погладить кота.",
    "Кот — проджект менеджер.",
    "Кот работает — не мешай.",
    "Очередь задач: 1. Корм. 2. Сон. 3. Код.",
    "Кот отказывается пушить.",
    "Всё уронит, но будет мило.",
    "Кот — наследие, баги — тоже.",
    "Кот — моя единственная точка роста.",
]

CLOSINGS = [
    "Кот из матрицы одобряет.",
    "P.S. Не забудь погладить кота.",
    "Мурррр.",
    "Шерсть повсюду — это любовь.",
    "Бип-буп-мяу.",
    "Код компилируется, кот мурлычет.",
    "Ctrl+Z в реальной жизни не работает.",
    "Лапки на клавиатуре — лучший code review.",
    "Кот в деле — сервера не падают.",
    "Мяу-контроль пройден.",
    "Система стабильна, кот доволен.",
    "Debug mode: отключён, мурчание: включено.",
    "Кот подтверждает: багов нет (только шерсть).",
    "Сервер пингован, кот накормлен.",
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
    "Напиши мемную подпись: кот ведёт себя как типичный айтишник.",
    "Придумай короткую мемную ситуацию: кот + клавиатура / ноутбук / сервер.",
    "Напиши смешное наблюдение: кот объясняет технологии по-своему.",
    "Придумай мем про кота, который думает что он сисадмин.",
    "Напиши подпись к мему: кот делает что-то глупое, а подаётся как IT-процесс.",
    "Придумай ироничную ситуацию: кот использует бытовой предмет как tech-гаджет.",
    "Напиши смешную строчку про кота и код / баги / деплой / прод.",
    "Придумай мем: кот саботирует работу, но выглядит как коллега.",
]


def _generate_llm() -> str | None:
    if not OPENROUTER_KEY:
        return None
    tried = set()
    user_prompt = random.choice(POST_PROMPTS)
    for attempt in range(min(len(OPENROUTER_MODELS), 3)):
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
                    "temperature": 0.8,
                    "max_tokens": 80,
                },
                timeout=30,
            )
            if resp.status_code == 429:
                log.warning("Rate limit (429) на %s, пропускаем", model)
                _model_index += 1
                time.sleep(2)
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
            time.sleep(1)
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

    pool = CAT_MEMES + MEME_ONELINERS
    available = [f for f in pool if f not in _used_facts]
    if not available:
        _used_facts.clear()
        available = pool

    fact = random.choice(available)
    emoji = random.choice(EMOJIS)
    _used_facts.add(fact)
    return f"{emoji} {fact}"


def _generate_one() -> tuple[str, str | None]:
    # 70% берём из мемного пула, 30% пробуем LLM
    if random.random() < 0.7:
        post = _generate_template()
    else:
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
        pool = CAT_MEMES + MEME_ONELINERS
        source = "template" if post and any(f in post for f in pool) else "generated"
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
    """Self-ping каждые 5 минут чтобы Render free не заснул."""
    while True:
        await asyncio.sleep(300)
        try:
            url = RENDER_URL or f"http://localhost:{PORT}"
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{url}/health", timeout=10) as resp:
                    log.info("Self-ping: %s", resp.status)
        except Exception as e:
            log.debug("Self-ping: %s", e)


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
