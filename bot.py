#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Монитор новых сделок публичного портфеля snowball-income.com → Telegram.

Один файл. Зависимости: requests, beautifulsoup4, lxml (+ playwright для браузера).

Бот двусторонний: шлёт уведомления и отвечает на команды /status, /last, /test,
/help, /stop. Команды он читает во время своего запуска, поэтому ответ приходит в
течении 5 минут — это не мгновенный чат, а почтовый ящик (бот живёт в GitHub
Actions и вне расписания не работает).

Про окно просмотра. Сайт отдаёт таблицу постранично: по умолчанию 25 строк из
нескольких тысяч. Поэтому бот (а) ставит размер страницы 100 и (б) при признаках
затора листает ещё страницы. Всё, что не успели разослать, остаётся в очереди
pending в state.json и доезжает следующим запуском — сделки не теряются. Если
накопилось больше CATCHUP_LIMIT сделок (простой, отпуск, первый запуск), хвост
приходит одним сводным сообщением, а не лентой.

Файлы рядом со скриптом:
  portfolio.txt — ссылка на публичный портфель (первая непустая строка без #)
  chat_id.txt   — chat_id получателя (необязательно)
  state.json    — отпечатки, счётчик портфеля, подписчики, очередь, offset

Переменные окружения: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS, PORTFOLIO_URL,
USE_BROWSER (0|1), STATE_FILE, DEAL_PAGES (страниц за прогон, по умолчанию 1).

Формат для GitHub Actions: печатает RESULT new=<n> error="<текст>"; код возврата
0 при успехе, 1 при поломке источника, 3 при нехватке настроек.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DEFAULT_URL = "https://snowball-income.com/public/portfolios/ukbRjaXjfg"
# подписи колонок таблицы сделок; у начислений они другие — отсеиваем по ним
HEADS = ["Операция", "Актив", "Дата", "Количество", "Цена", "Комиссия", "Сумма", "Прибыль"]
PAGE_SIZE = "100"         # размер страницы на сайте: 25 | 50 | 100
DEAL_PAGES = 1            # сколько страниц листаить в спокойном режиме
DEAL_PAGES_DEEP = 3       # ...и когда виден затор (100·3 = 300 сделок под окном)
MAX_PER_RUN = 40          # уведомлений за запуск; остаток — в pending, не теряется
CATCHUP_LIMIT = 12        # больше — это «догон после простоя», а не живая лента
CATCHUP_DIGEST = 25       # сколько строк влезает в одно сводное сообщение
MAX_SEEN = 3000           # сколько отпечатков хранить
MAX_PENDING = 600         # потолок очереди, чтобы state.json не разрастался
WARN_COOLDOWN = 6 * 3600  # как часто напоминать о поломке источника
MISSED_COOLDOWN = 1800    # как часто говорить о пропущенных за окном сделках
MAX_CMDS = 15             # сколько команд разобрать за один запуск
SEND_GAP = 1.05           # Telegram позволяет примерно одно сообщение в секунду
TZ_HOURS = 3              # МСК: в Actions часы UTC, иначе штампы путают человека

log = logging.getLogger("bot")


def esc(text: str) -> str:
    """Экран для Telegram parse_mode=HTML."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def conf(name: str) -> str:
    """Первая «живая» строка файла: пустые и с # в начале пропускаются."""
    path = HERE / name
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip().strip('"').strip("'")
        if line and not line.startswith("#"):
            return line
    return ""


def portfolio_url() -> str:
    return (os.environ.get("PORTFOLIO_URL") or conf("portfolio.txt") or DEFAULT_URL).strip()


def share_key(url: str) -> str:
    m = re.search(r"portfolios/([A-Za-z0-9]+)", url or "")
    return m.group(1) if m else ""


def pages_wanted(state: dict) -> int:
    """Глубина просмотра: обычно одна страница, при заторе — глубже."""
    env = os.environ.get("DEAL_PAGES")
    if env and env.isdigit():
        return max(1, min(10, int(env)))
    return DEAL_PAGES_DEEP if state.get("deep") else DEAL_PAGES


# -------------------------------------------------------------------------- #
#  Разбор страницы
# -------------------------------------------------------------------------- #
def th_label(th) -> str:
    """Подпись колонки <th>.

    В отрендеренном браузере DOM внутри <th> лежит SVG-иконка сортировки с
    <title>Created with Sketch.</title>, из-за чего get_text() даёт
    "Операция Created with Sketch." и сравнение заголовков не совпадает.
    Берём только собственные текстовые узлы ячейки.
    """
    parts = []
    for node in th.find_all(string=True):
        if node.name:                                   # <title> внутри <svg> и т.п.
            continue
        parent = node.parent
        if parent is not None and (parent.name in ("svg", "title", "style", "script")
                                   or parent.find_parent("svg") is not None):
            continue
        for line in node.split("\n"):
            line = line.replace("\xa0", " ").strip()
            if line:
                parts.append(line)
    return " ".join(parts)


def _cell_lines(cell) -> list[str]:
    return [t.strip() for t in cell.get_text("\n", strip=True).split("\n") if t.strip()]


def _first(lines: list[str], i: int) -> str:
    """Первая строка i-й ячейки строки таблицы."""
    return lines[i][0] if i < len(lines) and lines[i] else ""


def parse_rows(html: str) -> list[dict]:
    """Строки таблицы сделок: актив, ISIN, дата, количество, цена, комиссия,
    сумма, прибыль (% и в рублях)."""
    rows: list[dict] = []
    for table in BeautifulSoup(html, "lxml").find_all("table"):
        heads = [th_label(th) for th in table.find_all("th")]
        if len(heads) < len(HEADS):
            continue
        if any(not h.startswith(want) for h, want in zip(heads, HEADS)):
            continue                                    # таблица начислений, не сделок
        body = table.find("tbody")
        if body is None:
            continue
        for tr in body.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < len(HEADS):
                continue
            lines = [_cell_lines(td) for td in cells]
            profit = lines[7] if len(lines) > 7 else []
            isin = ""
            m = re.search(r"\b(RU|US|KYG|XS|NL)\w{6,12}\b", " ".join(lines[1]) if len(lines) > 1 else "")
            if m:
                isin = m.group(0)
            row = {"operation": _first(lines, 0), "asset": _first(lines, 1), "isin": isin,
                   "date": _first(lines, 2), "qty": _first(lines, 3), "price": _first(lines, 4),
                   "commission": _first(lines, 5), "amount": _first(lines, 6),
                   "profit_pct": profit[0] if profit else "",
                   "profit_abs": profit[1] if len(profit) > 1 else ""}
            if row["operation"] and row["asset"]:
                rows.append(row)
    return rows


def parse_meta(html: str) -> dict:
    """Метаданные из SSR-блока __NEXT_DATA__. Читаются даже без браузера —
    это резервный канал: по transactionsCount видно, что сделки появились."""
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  html, re.S)
    if not m:
        return {}
    try:
        p = json.loads(m.group(1))["props"]["pageProps"]["portfolio"] or {}
    except Exception:
        return {}
    return {"name": p.get("name") or "",
            "count": p.get("transactionsCount"),
            "last_date": (p.get("lastTransactionDate") or "")[:10]}


def pagination_total(html: str) -> int | None:
    """Число N из подписи «Показано 1-100 из N». Нужна, чтобы честно сказать,
    остались ли сделки под нижней границей окна просмотра."""
    m = re.search(r"Показано\s+\d+[-–]\d+\s+из\s+([\d\s\u00a0\u2009]+)", html)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def _is_deals_table(table) -> bool:
    heads = [th_label(th) for th in table.find_all("th")]
    return len(heads) >= len(HEADS) and all(
        h.startswith(want) for h, want in zip(heads, HEADS))


def deals_total(html: str) -> int | None:
    """Сколько всего сделок в ТАБЛИЦЕ СДЕЛОК («Показано 1-100 из 3633»).

    На странице два таких счётчика: у сделок и у начислений (1786), поэтому ищем
    текст внутри контейнера именно нужной таблицы, а не первый по документу.
    """
    for table in BeautifulSoup(html, "lxml").find_all("table"):
        if not _is_deals_table(table):
            continue
        node = table
        for _ in range(8):
            node = node.parent
            if node is None:
                break
            total = pagination_total(node.get_text(" ", strip=True))
            if total:
                return total
        break
    return None


def describe(html: str) -> str:
    """Одна строка о том, что реально пришло. Чтобы не гадать по логу."""
    if not html:
        return "пустой ответ (0 байт)"
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    rows = sum(len((t.find("tbody") or t).find_all("tr")) for t in tables)
    title = soup.title.get_text(strip=True) if soup.title else "без <title>"
    out = [f"длина={len(html)}", f"title={title!r}", f"таблиц={len(tables)}", f"строк={rows}",
           "__NEXT_DATA__=" + ("есть" if "__NEXT_DATA__" in html else "НЕТ")]
    low = html[:6000].lower()
    for mark, label in (("captcha", "похоже на капчу"), ("attention required", "проверка Cloudflare"),
                        ("403 forbidden", "403"), ("доступ ограничен", "доступ ограничен")):
        if mark in low:
            out.append(label)
    return "; ".join(out)


def row_key(row: dict, portfolio: str) -> str:
    """Отпечаток сделки.

    Две одинаковые сделки одного дня (частый случай у брокера) дают одинаковый
    отпечаток, поэтому в seen хранится не просто «видели», а СКОЛЬКО копий этого
    отпечатка мы уже разослали — иначе вторая сделка молча пропадала.
    """
    body = "|".join(str(row.get(k, "")) for k in
                    ("operation", "asset", "isin", "date", "qty", "price",
                     "commission", "amount", "profit_abs"))
    return hashlib.sha1(f"{portfolio}|{body}".encode("utf-8")).hexdigest()[:16]


# -------------------------------------------------------------------------- #
#  Получение HTML
# -------------------------------------------------------------------------- #
def fetch_http(url: str) -> str:
    r = requests.get(url, timeout=40,
                     headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"})
    r.raise_for_status()
    return r.text


SET_SIZE_JS = """(v) => { let done = false;
  for (const s of document.querySelectorAll('select')) {
    if ([...s.options].some(o => o.value === v)) {
      const set = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
      set.call(s, v); s.dispatchEvent(new Event('change', {bubbles: true})); done = true;
    }
  }
  return done; }"""

CLICK_PAGE_JS = """(n) => { const el = [...document.querySelectorAll('a,button')]
    .find(e => e.innerText.trim() === String(n) && /btn-icon|page/i.test(e.className + ' ' +
             (e.parentElement ? e.parentElement.className : '')));
  if (el) { el.click(); return true; } return false; }"""


def fetch_browser(url: str, pages: int = 1, size: str = PAGE_SIZE) -> str:
    """Рендерит страницу, разворачивает размер страницы и листает нужное число
    страниц. Возвращает склеенный HTML — parse_rows сам найдёт все таблицы.

    networkidle недостаточно: таблица догружается только после перехода на
    вкладку #transactions, поэтому кликаем таб и ждём содержимое, а не загрузку.
    Размер страницы по умолчанию 25 — этого на активный день не хватает, поэтому
    ставим 100 (сайт поддерживает 25/50/100) и при необходимости идём глубже.
    """
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage",
                                     "--disable-blink-features=AutomationControlled"])
        pg = b.new_context(user_agent=UA, locale="ru-RU", timezone_id="Europe/Moscow",
                           viewport={"width": 1600, "height": 1600}).new_page()
        chunks: list[str] = []
        try:
            pg.goto(url + "#transactions", wait_until="domcontentloaded", timeout=90_000)
            for sel in ('a[href$="#transactions"]', 'button:has-text("Сделки")',
                        '[role="tab"]:has-text("Сделки")'):
                try:
                    el = pg.locator(sel).first
                    if el.count() > 0:
                        el.click(timeout=4000)
                        break
                except Exception:
                    continue
            try:
                pg.wait_for_function(
                    "() => [...document.querySelectorAll('tbody tr')].some(r => "
                    "/Покупка|Продажа/.test(r.innerText || '') "
                    "&& r.querySelectorAll('td').length >= 8)", timeout=25_000)
            except Exception as e:
                log.debug("строк не дождались: %s", e)
            try:                                        # 100 строк вместо 25
                if pg.evaluate(SET_SIZE_JS, size):
                    pg.wait_for_timeout(2500)
            except Exception as e:
                log.debug("размер страницы не поменяли: %s", str(e)[:120])
            chunks.append(pg.content())
            for n in range(2, max(1, pages) + 1):
                try:
                    if not pg.evaluate(CLICK_PAGE_JS, n):
                        log.debug("страницы %d нет", n)
                        break
                    pg.wait_for_timeout(2200)
                    chunks.append(pg.content())
                except Exception as e:
                    log.debug("листать дальше не вышло: %s", str(e)[:120])
                    break
            return "\n".join(chunks)
        finally:
            b.close()


def get_page(url: str, use_browser: bool, pages: int = 1) -> tuple[str, str, str]:
    """Возвращает (html, how, diag). how='' если строк нет; diag — что пришло."""
    ways = ["browser", "browser", "http"] if use_browser else ["http"]
    last_how, diag = "", ""
    for how in ways:
        try:
            html = fetch_browser(url, pages) if how == "browser" else fetch_http(url)
        except Exception as e:
            log.warning("способ %s не удался: %s", how, str(e)[:200])
            continue
        last_how = how
        if parse_rows(html):
            return html, how, ""
        diag = describe(html)
        log.warning("строк нет (%s). Ответ: %s", how, diag)
        time.sleep(2)
    return "", last_how or "http", diag or "ни один способ не вернул HTML"


# -------------------------------------------------------------------------- #
#  Telegram: отправка и приём команд
# -------------------------------------------------------------------------- #
def api(token: str, method: str, **data):
    """Вызов Bot API: (ok, result|description).

    429 обрабатываем здесь, а не на уровне вызова: лимит Telegram на сообщения
    в один чат — примерно одно в секунду, и без паузы часть уведомлений просто
    отваливалась бы с «Too Many Requests» и терялась навсегда.
    """
    wait = 0
    for attempt in range(4):
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                              data=data, timeout=45)
            j = r.json()
        except Exception as e:
            if attempt < 3:
                time.sleep(min(30, 5 * (attempt + 1)))
                continue
            return False, f"HTTP-ошибка: {str(e)[:120]}"
        if j.get("ok"):
            return True, j.get("result")
        desc = str(j.get("description", "unknown"))
        m = re.search(r"retry after (\d+)", desc, re.I)
        wait = min(60, int(m.group(1)) if m else 5 * (attempt + 1))
        if "too many requests" not in desc.lower() or attempt == 3:
            return False, desc
        log.warning("лимит Telegram, жду %d с (попытка %d)", wait, attempt + 1)
        time.sleep(wait + 1)
    return False, "слишком много попыток"


def tg_send(token: str, chat_id: str, text: str) -> None:
    ok, err = api(token, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                  disable_web_page_preview="true")
    if not ok:
        raise RuntimeError(f"sendMessage: {err}")


def drop_webhook(token: str) -> None:
    """Если когда-то был назначен webhook, Telegram перестанет отдавать
    getUpdates — снимаем его, иначе команды до бота не дойдут."""
    ok, err = api(token, "deleteWebhook", drop_pending_updates="false")
    if not ok:
        log.warning("deleteWebhook: %s", str(err)[:120])


def parse_command(text: str) -> tuple[str, str]:
    """'/last 5' → ('last','5'); '/start@my_bot' → ('start','').

    В группах Telegram прилагает к команде суффикс @имя_бота, поэтому его срезаем
    всегда и без знания имени бота — иначе в группе команды перестают работать.
    """
    t = (text or "").strip()
    if not t.startswith("/"):
        return "", ""
    parts = t.split(None, 1)                   # "/last 3" — аргумент на той же строке
    cmd = parts[0][1:].split("@", 1)[0]        # "/status@name_bot" → "status"
    return cmd.lower(), (parts[1].strip() if len(parts) > 1 else "")


def get_updates(token: str, after_id: int) -> tuple[list[dict], int, str]:
    """Команды от пользователей, пришедшие после after_id.

    timeout=0: long polling в cron бессмысленен, нам нужен мгновенный снимок.
    Возвращает (сообщения, новый offset, ошибку).
    """
    ok, res = api(token, "getUpdates", offset=after_id + 1, timeout=0, limit=50)
    if not ok:
        return [], after_id, str(res)
    msgs: list[dict] = []
    new_id = after_id
    for upd in res or []:
        uid = int(upd.get("update_id") or 0)
        new_id = max(new_id, uid)
        msg = upd.get("message") or upd.get("channel_post")
        if msg and msg.get("text"):
            msgs.append({"id": uid, "chat": str((msg.get("chat") or {}).get("id") or ""),
                         "user": (msg.get("from") or {}).get("username") or "",
                         "text": msg["text"]})
    return msgs, new_id, ""


HELP = """🤖 <b>Как пользоваться</b>

Бот запускается раз в 5 минут, поэтому отвечает не мгновенно, а в течение этого шага.

/status — жив ли мониторинг, что в очереди на отправку
/last — последние 5 сделок
/test — проверить доставку: шлёт последнюю сделку как уведомление
/stop — отписаться (вернуть — /start)
/help — этот список"""


def status_text(state: dict, name: str, rows: list[dict], url: str) -> str:
    ago = state.get("last_ok")
    gap = int(time.time() - ago) // 60 if isinstance(ago, int) else None
    L = ["📊 <b>Статус мониторинга</b>", ""]
    L.append(f"Портфель: <b>{esc(name)}</b>")
    cnt = state.get("total") or state.get("count")
    L.append(f"Сделок всего: <b>{esc(str(cnt)) if cnt is not None else '—'}</b>")
    if state.get("last_date"):
        L.append(f"Дата последней сделки: {esc(str(state['last_date']))}")
    L.append(f"Видно строк в окне: <b>{len(rows)}</b>"
             + (f" из {state['site_total']} на сайте" if state.get("site_total") else "")
             + (f" · страниц: {state.get('pages') or 1}" if (state.get("pages") or 1) > 1 else ""))
    pend = len(state.get("pending") or [])
    L.append(f"В очереди на отправку: <b>{pend}</b>"
             + ("" if not pend else f" — разойдётся за {max(1, -(-pend // MAX_PER_RUN))} прог."))
    L.append(f"Отпечатков в базе: {len(state.get('seen') or {})}")
    if state.get("stale_dropped"):
        L.append(f"Отброшено устаревших строк: {state['stale_dropped']} "
                 "(сделку поправили/удалили до отправки)")
    L.append(f"Источник: <b>{esc(str(state.get('how') or '—'))}</b> · "
             f"{esc('OK' if not state.get('error') else 'сбой')}")
    if gap is not None:
        L.append(f"Прошлый запуск: {gap} мин назад"
                 + ("  ⚠️ GitHub Actions будил реже, чем по расписанию" if gap > 15 else ""))
    if state.get("error"):
        L += ["", f"⚠️ {esc(str(state['error'])[:220])}"]
    L += ["", f'<a href="{url}#transactions">Открыть сделки портфеля</a>']
    return "\n".join(L)


def run_cmd(cmd: str, arg: str, state: dict, *, name: str, rows: list[dict], url: str,
            how: str, chat: str) -> tuple[str, bool]:
    """Обработка одной команды. Возвращает (ответ, надо_ли_сохранить_состояние)."""
    pairs = state.setdefault("pairs", [])
    if chat not in pairs:                       # первая команда = подписка
        pairs.append(chat)
        state["pairs"] = pairs
        return ("✅ Подписка оформлена: присылаю новые сделки портфеля "
                f"<b>{esc(name)}</b>.\n\n" + HELP, True)
    if cmd == "stop":
        muted = state.setdefault("muted", [])
        if chat not in muted:
            muted.append(chat)
        return ("🔒 Отписался: уведомлений не шлю, проверка продолжается. "
                "Вернуть — /start.", True)
    if cmd == "start":
        muted = state.setdefault("muted", [])
        if chat in muted:
            muted.remove(chat)
            return ("✅ Снова подписан на уведомления.\n\n" + HELP, True)
        return ("✅ Мониторинг активен, ты уже подписан.\n\n" + HELP, False)
    if cmd == "help":
        return (HELP, False)
    if cmd == "status":
        return (status_text(state, name, rows, url), False)
    nodata = ("⚠️ Таблицу сделок сейчас не удалось разобрать, показать нечего.\n"
              + status_text(state, name, rows, url))
    if cmd in ("test", "проверка"):
        if not rows:
            return (nodata, False)
        return ("<b>Тестовое уведомление</b> (это не новая сделка)\n\n"
                + message(rows[0], name, url + "#transactions",
                          "#" + row_key(rows[0], name)[:8] + " · " + stamp()), False)
    if cmd == "last":
        try:
            n = max(1, min(10, int(arg or 5)))
        except ValueError:
            return (f"Не понял число: {esc(arg)}. Пример: /last 3", False)
        if not rows:
            return (nodata, False)
        out = [f"📑 <b>Последние {min(n, len(rows))} сделок</b> — {esc(name)}"]
        for i, r in enumerate(rows[:n], 1):
            line = " ".join(x for x in (r.get("operation", ""), r.get("asset", ""),
                                        r.get("date", ""), r.get("amount", "")) if x)
            out.append(f"{i}. {esc(line)}")
        out += ["", "Одну сделку целиком: /test"]
        return ("\n".join(out), False)
    if cmd == "queue":
        q = state.get("pending") or []
        if not q:
            return ("Очередь пуста — всё разослано.", False)
        out = [f"⏳ <b>В очереди {len(q)}</b>, в этом запуске уйдёт {min(len(q), MAX_PER_RUN)}"]
        for i, r in enumerate(q[:5], 1):
            out.append(f"{i}. {esc(' '.join(x for x in (r.get('operation', ''), r.get('asset', ''), r.get('date', '')) if x))}")
        return ("\n".join(out), False)
    return (f"Неизвестная команда: /{esc(cmd)}\n\n{HELP}", False)


# -------------------------------------------------------------------------- #
#  Сообщение об одной сделке
# -------------------------------------------------------------------------- #
def stamp() -> str:
    """«07.09 15:41 МСК» — момент, когда сделка была видна на сайте.

    Нужен, чтобы отличить «бот соврал» от «сделку поправили или удалили после
    проверки»: у портфеля руки владельца, правки там обычное дело.
    """
    t = time.gmtime(time.time() + TZ_HOURS * 3600)
    return time.strftime("%d.%m %H:%M", t) + " МСК"


def message(row: dict, portfolio: str, url: str, note: str = "") -> str:
    op = row.get("operation", "")
    icon = {"покупка": "🟢", "продажа": "🔴"}.get(op.lower(), "⚪️")
    L = [f"{icon} <b>{esc(op)}</b> — {esc(portfolio)}", ""]
    L.append(f"<b>{esc(row.get('asset', ''))}</b>"
             + (f"  <code>{esc(row['isin'])}</code>" if row.get("isin") else ""))
    for label, k in (("Дата", "date"), ("Количество", "qty"), ("Цена", "price"),
                     ("Комиссия", "commission"), ("Сумма", "amount")):
        row[k] = row.get(k, "")                # отложенные сделки приходят из JSON
        if row[k] and row[k] != "-":
            L.append(f"{label}: <b>{esc(row[k])}</b>")
    profit = " ".join(x for x in (row.get("profit_pct", ""), row.get("profit_abs", ""))
                      if x and x != "-")
    if profit:
        L.append(f"Прибыль: <b>{esc(profit)}</b>")
    L += ["", f'<a href="{url}">Открыть сделки портфеля</a>',
          f"<i>проверено {stamp()}" + (f" · {note}" if note else "") + "</i>"]
    return "\n".join(L)


# -------------------------------------------------------------------------- #
#  Состояние
# -------------------------------------------------------------------------- #
def load_state(path: Path) -> dict:
    state = {"seen": {}, "count": None, "baseline": False, "warn_at": 0,
             "pairs": [], "upd": 0, "done": [], "error": "", "how": "", "last_ok": 0,
             "pending": [], "deep": False, "pages": 1}
    if path.exists():
        try:
            state.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            log.warning("state.json повреждён — начинаю с чистого листа")
    for key, default in (("seen", {}), ("pairs", []), ("done", []), ("pending", [])):
        state.setdefault(key, default)
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# -------------------------------------------------------------------------- #
#  Один цикл
# -------------------------------------------------------------------------- #
def clamp_seen(rows: list[dict], seen: dict, portfolio: str) -> None:
    """Сверяем счётчики отправленного с тем, что реально в окне.

    Удалили сделку на сайте (или её исправили и завели заново) — копий в окне
    стало меньше, чем мы отослали. Возвращаем счётчик к фактическому числу, иначе
    «исправленная» сделка не пришла бы никогда. Ключей, которых в окне нет, не
    касаемся: они просто выехали за нижнюю границу.
    """
    occ = Counter(row_key(r, portfolio) for r in rows)
    for k, c in occ.items():
        if k in seen and c < int(seen[k] or 0):
            seen[k] = c


def queue_fresh(rows: list[dict], seen: dict, state: dict, portfolio: str
                ) -> tuple[list[dict], int]:
    """Что отправить: очередь прошлого запуска + свежие сделки этой страницы.

    Свежесть считается ПО КОЛИЧЕСТВУ копий отпечатка, а не по факту «видели/нет»:
    две одинаковые сделки одного дня иначе сливались в одну.

    Отложенные строки ОБЯЗАНЫ всё ещё быть на сайте. Это лечит «бот прислал
    сделку, которой нет»: строка могла пролежать в очереди до следующего
    запуска, а владелец за это время её поправил или удалил — и мы бы рассказали
    о том, чего уже не существует. Такое молча выбрасываем и говорим отдельно.
    """
    live = Counter(row_key(r, portfolio) for r in rows)
    queue: list[dict] = []
    stale = 0
    for r in state.get("pending") or []:
        if not isinstance(r, dict):
            continue
        if live.get(row_key(r, portfolio), 0) <= 0:
            stale += 1                          # на сайте её больше нет
            continue
        queue.append(r)
    queued = Counter(row_key(r, portfolio) for r in queue)
    sent = Counter({k: int(v or 1) for k, v in (seen or {}).items()})
    occ = Counter()
    for r in rows:
        k = row_key(r, portfolio)
        occ[k] += 1
        if occ[k] > sent.get(k, 0) + queued.get(k, 0):
            queue.append(r)
            queued[k] += 1
    return queue[:MAX_PENDING], stale


def digest_rows(rows: list[dict], name: str, limit: int = CATCHUP_DIGEST) -> str:
    """Компактный список сделок вместо шквала отдельных уведомлений."""
    out = []
    for i, r in enumerate(rows[:limit], 1):
        line = " ".join(x for x in (r.get("date", ""), r.get("operation", ""),
                                    r.get("asset", ""), r.get("amount", "")) if x)
        out.append(f"{i}. {esc(line)}")
    if len(rows) > limit:
        out.append(f"…и ещё {len(rows) - limit}")
    dates = [r.get("date", "") for r in rows if r.get("date")]
    if dates:
        out.append("")
        out.append(f"Период: {min(dates, key=_dkey)} — {max(dates, key=_dkey)}, "
                   "ищите не только на сегодня: на сайте это ниже по списку")
    return "\n".join(out)


def _dkey(d: str) -> str:
    """Дата «07/09/2026» (на сайте слэши) -> «20260907» для сравнения строк.

    Без нормализации min/max сравнивали бы день с месяцем и «Период» в сводке
    показывал бы неверные границы — ровно то, из-за чего сделки «исчезают».
    """
    p = [x for x in re.split(r"[./\-]", (d or "").strip()) if x]
    if len(p) == 3 and all(x.isdigit() for x in p):
        return f"{p[2]}{p[1].zfill(2)}{p[0].zfill(2)}"
    return d or ""


def run(token: str, chats: list[str], url: str, use_browser: bool, state_path: Path,
        page=None) -> tuple[int, int, str]:
    """Возвращает (код_выхода, число_новых, ошибку). page — get_page, для тестов."""
    state = load_state(state_path)
    portfolio = share_key(url)
    name = state.get("name") or portfolio
    dirty = {"v": False}

    # получатели из секрета подписаны по определению: иначе первая команда от них
    # ушла бы в «оформить подписку» вместо ответа на /status
    merged_chats = list(dict.fromkeys([*chats, *(state.get("pairs") or [])]))
    if merged_chats != (state.get("pairs") or []):
        state["pairs"] = merged_chats
        dirty["v"] = True

    def targets() -> list[str]:
        muted = set(state.get("muted") or [])
        return [c for c in dict.fromkeys([*chats, *(state.get("pairs") or [])]) if c not in muted]

    # Разные смыслы у «нечему слать»:
    #  · подписанты есть, но все отписались (/stop) — сделки считаем неинтересными,
    #    помечаем отправленными, очередь не раздуваем;
    #  · подписантов нет вообще (не задан TELEGRAM_CHAT_IDS и не написали боту) —
    #    копит очередь, чтобы после /start человек получил их целиком, но с потолком.
    subscribed = list(dict.fromkeys([*chats, *(state.get("pairs") or [])]))
    opted_out = bool(subscribed) and not targets()
    if not subscribed and int(time.time()) - int(state.get("nopair_at") or 0) > WARN_COOLDOWN:
        state["nopair_at"] = int(time.time())
        save_state(state_path, state)
        print("⚠️ Получателей нет: TELEGRAM_CHAT_IDS пуст и боту никто не написал. "
              "Сделки копятся в очереди — напишите боту /start, и они придут сводкой.",
              file=sys.stderr)

    def send(text: str, only: str = "") -> str:
        """Одно сообщение всем адресатам: 'ok', 'fatal' или 'retry'.

        fatal — чат нас заблокировал/удалён, повторять бессмысленно;
        retry — 429/сеть, такое сообщение остаётся в очереди на следующий запуск.
        """
        if not only and not targets():
            # получателей нет вовсе (не задан секрет и боту не написали) — сделку НЕ
            # считаем доставленной: она дождётся подписки и придёт сводкой
            return "retry"
        outcome, fatal = "ok", False
        for cid in ([only] if only else targets()):
            if not cid:
                continue
            try:
                tg_send(token, cid, text)
            except Exception as e:
                txt = str(e)
                outcome = "retry"
                if any(x in txt for x in ("blocked", "Forbidden", "chat not found",
                                          "PEER_INVALID", "kicked")):
                    fatal = True
                log.error("не отправил в %s: %s", cid, txt[:200])
            time.sleep(SEND_GAP)
        return "fatal" if fatal else ("ok" if outcome == "ok" else "retry")

    def flush() -> None:
        if dirty["v"]:
            save_state(state_path, state)
            dirty["v"] = False

    # --- страница (окно просмотра) ---
    pages = pages_wanted(state)
    state["pages"] = pages
    rows: list[dict] = []
    meta: dict = {}
    html, how, diag = (page or get_page)(url, use_browser, pages)
    if html:
        meta = parse_meta(html)
        name = meta.get("name") or name
        state["name"] = name
        rows = parse_rows(html)
    state["how"] = how
    state["error"] = "" if html else (diag or "нет HTML")[:220]

    # --- команды ---
    msgs, upd, upd_err = get_updates(token, int(state.get("upd") or 0))
    if upd_err:
        log.warning("getUpdates недоступен: %s", upd_err[:160])
    if upd > int(state.get("upd") or 0):
        state["upd"] = upd
        dirty["v"] = True

    done = {int(x) for x in state.get("done") or []}
    for m in [x for x in msgs if x["id"] not in done][:MAX_CMDS]:
        cmd, arg = parse_command(m["text"])
        chat = m["chat"]
        if not cmd:
            done.add(m["id"])
            continue
        if chats and chat not in chats and chat not in (state.get("pairs") or []):
            answer = ("⚠️ Мониторинг работает, но этот чат не в списке получателей "
                      "(секрет TELEGRAM_CHAT_IDS). Добавь chat_id в секрет — и я начну "
                      "присылать сделки сюда.")
        else:
            answer, changed = run_cmd(cmd, arg, state, name=name, rows=rows,
                                      url=url, how=how, chat=chat)
            dirty["v"] = dirty["v"] or changed
        send(answer, only=chat)
        done.add(m["id"])
    state["done"] = list(done)[-400:]
    flush()

    # --- случай 1: таблицу не вытащить: хотя бы счётчик ---
    if not html:
        if not meta:
            try:
                meta = parse_meta(fetch_http(url))
            except Exception:
                pass
        prev, cur = state.get("count"), meta.get("count")
        if isinstance(prev, int) and isinstance(cur, int) and cur > prev:
            send("🔔 <b>В портфеле появились сделки</b>\n\n"
                 f"Сделок всего: {prev} → <b>{cur}</b> (+{cur - prev})\n"
                 f"Последняя дата сделки: {esc(str(meta.get('last_date')))}\n\n"
                 "⚠️ Параметры вытащить не удалось, поэтому тексты сделок придут "
                 "позже одним блоком. /status покажет очередь.")
            state["count"] = cur
            state["deep"] = True          # следующий прогон посмотрит глубже
            state["last_ok"] = int(time.time())
            save_state(state_path, state)
            return 0, cur - prev, "details unavailable"
        now = int(time.time())
        if now - int(state.get("warn_at") or 0) > WARN_COOLDOWN:
            send("⚠️ <b>Мониторинг не видит сделок</b>\n\n"
                 f"browser={use_browser}, способ: {esc(how or '—')}\n"
                 f"Что пришло в ответе: {esc(diag)}\n\n"
                 "Частые причины: сайт закрылся от адресов дата-центра (капча), "
                 "Chromium не стартовал, или файл скрипта повреждён.\n"
                 "/status покажет, в чём дело")
            state["warn_at"] = now
        state["last_ok"] = int(time.time())
        save_state(state_path, state)
        return 1, 0, "empty result"

    # --- случай 2: строки есть ---
    state["last_ok"] = int(time.time())
    # счётчик и дата — ДО baseline: нужны и в приветствии, и в канале
    # «сделки появились», и в проверке «не вышли ли за окно»
    prev_count = state.get("count")
    state["count"] = meta.get("count") or prev_count
    state["last_date"] = meta.get("last_date") or state.get("last_date")

    if not state.get("baseline"):
        state["seen"] = dict(Counter(row_key(r, name) for r in rows))
        state["total"] = deals_total(html) or pagination_total(html) or meta.get("count")
        state["pending"] = []
        state["baseline"] = True
        save_state(state_path, state)
        send("✅ <b>Мониторинг запущен</b>\n\n"
             f"Портфель: <b>{esc(name)}</b>\n"
             f"Сделок всего: {esc(str(state.get('count')))}\n"
             f"Последняя дата сделки: {esc(str(meta.get('last_date')))}\n"
             f"Окно просмотра: {len(rows)} строк, страниц: {pages}, "
             f"источник: {esc(how)}\n\n"
             "Дальше присылаю только новые покупки и продажи. Если поток сделок "
             f"больше окна, окно само расширится до {DEAL_PAGES_DEEP} страниц.\n" + HELP)
        return 0, 0, ""

    pending_before = len(state.get("pending") or [])
    clamp_seen(rows, state["seen"], name)
    queue, stale = queue_fresh(rows, state["seen"], state, name)
    if stale:
        log.warning("отброшено %d отложенных строк: на сайте их уже нет", stale)
        state["stale_dropped"] = int(state.get("stale_dropped") or 0) + stale

    # Догон после простоя/апдейта: лента из десятков старых сделок бесполезна,
    # поэтому свежие идут по одному сообщению, а хвост — одним списком. Ничего не
    # теряется: в сводке перечислены все строки, не попавшие в ленту.
    catchup = len(queue) > CATCHUP_LIMIT
    head = queue[:CATCHUP_LIMIT if catchup else MAX_PER_RUN]
    rest = list(queue[len(head):])
    delivered = skipped = digested = fails = 0
    for i, r in enumerate(head):
        k = row_key(r, name)
        if opted_out:                            # /stop: помечаем, не шлём, не копим
            state["seen"][k] = int(state["seen"].get(k, 0) or 0) + 1
            skipped += 1
            continue
        res = send(message(r, name, url + "#transactions", "#" + row_key(r, name)[:8]))
        if res in ("ok", "fatal"):
            state["seen"][k] = int(state["seen"].get(k, 0) or 0) + 1
            delivered += res == "ok"
        else:                                    # retry: не теряем, уезжает в хвост
            fails += 1
            rest.insert(0, r)
        if not catchup and fails >= 3:           # сеть легла — не крутим весь прогон
            rest[0:0] = head[i + 1:]
            break
    if catchup and rest and not opted_out:
        # Сводку режем на блоки: обрезок «…и ещё N» означал бы, что сделки
        # помечены отправленными, но человек про них не прочитал.
        chunks = [rest[i:i + CATCHUP_DIGEST] for i in range(0, len(rest), CATCHUP_DIGEST)][:4]
        for num, chunk in enumerate(chunks, 1):
            text = ("📦 <b>Разбор пропущенного</b> — " + esc(name) +
                    (f" (часть {num}/{len(chunks)})" if len(chunks) > 1 else "") + "\n\n"
                    f"Не разослано по одной {len(rest)} сделок (окно: {len(rows)} строк "
                    f"× {pages} стр.; пришло больше, чем влезает в ленту).\n\n"
                    + digest_rows(chunk, name)
                    + "\n\nПолные параметры: /last и /test.")
            if send(text) not in ("ok", "fatal"):
                break                        # chunk и всё, что после, остаётся в очереди
            for r in chunk:
                k = row_key(r, name)
                state["seen"][k] = int(state["seen"].get(k, 0) or 0) + 1
            digested += len(chunk)
        rest = rest[digested:]
    state["pending"] = rest[:MAX_PENDING]
    if skipped:
        log.info("вы отписались (/stop): %d новых сделок помечены без отправки", skipped)

    # Затор. Пришло больше сделок, чем мы нашли в окне, или всё окно оказалось
    # новым — значит за нижней его границей кто-то остался. В ответ — следующий
    # прогон листает глубже, а вас честно предупреждаем о возможной потере.
    # Прирост считаем по «Показано 1-100 из N» — это про текущую выдачу.
    # SSR-поле transactionsCount отстаёт (на живом портфеле: 3627 против 3637),
    # поэтому оно только для запасного канала, а не для детектора пропусков.
    live_total = deals_total(html) or pagination_total(html)
    state["site_total"] = live_total
    prev_total, cur_total = state.get("total"), live_total or state.get("count")
    if cur_total:
        state["total"] = cur_total
    grew = 0
    if isinstance(prev_total, int) and isinstance(cur_total, int):
        grew = max(0, cur_total - prev_total)
    fresh_now = len(queue) - pending_before
    # Предупреждаем только о том, что физически не влезло в окно. Раньше считали
    # от «новых строк» и пугали «могли не дойти 37» при приросте в 37 сделок на
    # окне в 100 строк — там как раз всё видно, а расхождение значило лишь, что
    # часть сделок уже была отправлена ранее.
    missed = max(0, grew - len(rows)) if grew else 0
    state["deep"] = bool(missed > 0 or (rows and fresh_now >= len(rows)))

    seen = dict(state["seen"])                 # LRU: коснёмся строк из окна
    for r in reversed(rows):                   # самые свежие — в конец, они не вытеснятся
        k = row_key(r, name)
        count = int(seen.pop(k, 0) or 0)
        seen[k] = count
    state["seen"] = {k: v for k, v in list(seen.items())[-MAX_SEEN:]}
    save_state(state_path, state)

    if missed > 0 and int(time.time()) - int(state.get("missed_at") or 0) > MISSED_COOLDOWN:
        send(f"⚠️ <b>Между проверками пришло {grew} сделок</b>, "
             f"а окно — всего {len(rows)} строк.\n"
             f"До {missed} сделок могли не дойти: они ушли под нижнюю границу окна.\n"
             f"Следующий прогон смотрит глубже ({DEAL_PAGES_DEEP} страницы ≈ "
             f"{int(PAGE_SIZE) * DEAL_PAGES_DEEP} сделок). Очередь: {len(state['pending'])}.")
        state["missed_at"] = int(time.time())
        save_state(state_path, state)

    if stale and int(time.time()) - int(state.get("stale_at") or 0) > MISSED_COOLDOWN:
        send(f"🧹 Отбросил {stale} отложенных сделок: на сайте их уже нет "
             "(владелец поправил или удалил портфель, пока ждала отправка).\n"
             "Это и есть «несуществующие сделки» — теперь такое не рассылаю.")
        state["stale_at"] = int(time.time())
        save_state(state_path, state)

    if rest:
        send(f"⏳ Разослал {delivered} из {len(queue)} новых сделок, "
             f"остальные {len(rest)} уйдут следующим запуском "
             f"(ждём ~{max(1, -(-len(rest) // MAX_PER_RUN))} × 5 мин).")
    if delivered:
        log.info("окно %d строк, разослано %d, в очереди осталось %d, источник %s",
                 len(rows), delivered, len(rest), how)
    return 0, delivered + digested, ("catchup not delivered" if (catchup and rest) else "")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    url = portfolio_url()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chats = [c.strip() for c in (os.environ.get("TELEGRAM_CHAT_IDS") or conf("chat_id.txt"))
             .split(",") if c.strip().lstrip("-").isdigit()]
    use_browser = os.environ.get("USE_BROWSER", "1") not in ("0", "false", "no")
    state_path = Path(os.environ.get("STATE_FILE") or HERE / "state.json")

    problems = []
    if not share_key(url):
        problems.append(f"в portfolio.txt нет ссылки вида {DEFAULT_URL}")
    if not token:
        problems.append("не задан секрет TELEGRAM_BOT_TOKEN")
    if problems:
        print("⛔ Запуск пропущен: " + "; ".join(problems), file=sys.stderr)
        return 3

    if not chats:
        log.info("chats пуст: подписка возможна командой /start из Telegram")
    drop_webhook(token)
    code, new, err = run(token, chats, url, use_browser, state_path)
    print(f'RESULT new={new} error={json.dumps(err, ensure_ascii=False)}')
    return code


if __name__ == "__main__":
    sys.exit(main())
