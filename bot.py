#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Монитор новых сделок публичного портфеля snowball-income.com → Telegram.

Один файл. Зависимости: requests, beautifulsoup4, lxml (+ playwright для браузера).

Файлы рядом со скриптом:
  portfolio.txt — ссылка на публичный портфель (первая непустая строка без #)
  chat_id.txt   — chat_id получателя (необязательно)
  state.json    — состояние: отпечатки отправленных сделок и счётчик портфеля

Переменные окружения: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS, PORTFOLIO_URL,
USE_BROWSER (0|1), STATE_FILE.

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
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DEFAULT_URL = "https://snowball-income.com/public/portfolios/ukbRjaXjfg"
# подписи колонок таблицы сделок; у начислений они другие — отсеиваем по ним
HEADS = ["Операция", "Актив", "Дата", "Количество", "Цена", "Комиссия", "Сумма", "Прибыль"]
MAX_PER_RUN = 10          # защита от шквала уведомлений
MAX_SEEN = 2000           # сколько отпечатков хранить
WARN_COOLDOWN = 6 * 3600  # как часто напоминать о поломке источника

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
    """Отпечаток сделки. Поля содержательные, поэтому порядок строк в выдаче и
    смена пагинации не приводят к повторной отправке."""
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


def fetch_browser(url: str) -> str:
    """Рендерит страницу и ждёт ПОЯВЛЕНИЯ СТРОК сделок.

    networkidle недостаточно: таблица догружается только после перехода на
    вкладку #transactions, поэтому кликаем таб и ждём содержимое, а не загрузку.
    """
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage",
                                     "--disable-blink-features=AutomationControlled"])
        pg = b.new_context(user_agent=UA, locale="ru-RU", timezone_id="Europe/Moscow",
                           viewport={"width": 1600, "height": 1200}).new_page()
        try:
            pg.goto(url + "#transactions", wait_until="domcontentloaded", timeout=90_000)
            try:
                pg.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass                                    # счётчики/вебсокеты мешают networkidle
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
            return pg.content()
        finally:
            b.close()


def get_page(url: str, use_browser: bool) -> tuple[str, str, str]:
    """Возвращает (html, how, diag). how='' если строк нет; diag — что пришло."""
    ways = ["browser", "browser", "http"] if use_browser else ["http"]
    last_how, diag = "", ""
    for how in ways:
        try:
            html = fetch_browser(url) if how == "browser" else fetch_http(url)
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
#  Telegram
# -------------------------------------------------------------------------- #
def tg_send(token: str, chat_id: str, text: str) -> None:
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": "true"}, timeout=45)
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"sendMessage: HTTP {r.status_code}")
    if not j.get("ok"):
        raise RuntimeError(f"sendMessage: {j.get('description', '')}")


def message(row: dict, portfolio: str, url: str) -> str:
    icon = {"покупка": "🟢", "продажа": "🔴"}.get(row["operation"].lower(), "⚪️")
    L = [f"{icon} <b>{esc(row['operation'])}</b> — {esc(portfolio)}", ""]
    L.append(f"<b>{esc(row['asset'])}</b>" + (f"  <code>{esc(row['isin'])}</code>" if row["isin"] else ""))
    for label, k in (("Дата", "date"), ("Количество", "qty"), ("Цена", "price"),
                     ("Комиссия", "commission"), ("Сумма", "amount")):
        if row[k] and row[k] != "-":
            L.append(f"{label}: <b>{esc(row[k])}</b>")
    profit = " ".join(x for x in (row["profit_pct"], row["profit_abs"]) if x and x != "-")
    if profit:
        L.append(f"Прибыль: <b>{esc(profit)}</b>")
    L += ["", f'<a href="{url}">Открыть сделки портфеля</a>']
    return "\n".join(L)


# -------------------------------------------------------------------------- #
#  Состояние
# -------------------------------------------------------------------------- #
def load_state(path: Path) -> dict:
    state = {"seen": {}, "count": None, "baseline": False, "warn_at": 0}
    if path.exists():
        try:
            state.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            log.warning("state.json повреждён — начинаю с чистого листа")
    state.setdefault("seen", {})
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# -------------------------------------------------------------------------- #
#  Один цикл
# -------------------------------------------------------------------------- #
def run(token: str, chats: list[str], url: str, use_browser: bool, state_path: Path) -> tuple[int, int, str]:
    """Возвращает (код_выхода, число_новых, ошибку)."""
    state = load_state(state_path)
    portfolio = share_key(url)

    def send(text: str) -> None:
        for cid in chats:
            try:
                tg_send(token, cid, text)
            except Exception as e:
                log.error("не отправил в %s: %s", cid, str(e)[:200])
            time.sleep(0.05)

    html, how, diag = get_page(url, use_browser)

    # --- случай 1: строк нет ---
    if not html:
        meta = {}
        try:                                     # пробуем вытащить хотя бы счётчик
            meta = parse_meta(fetch_http(url))
        except Exception:
            pass
        prev, cur = state.get("count"), meta.get("count")
        if isinstance(prev, int) and isinstance(cur, int) and cur > prev:
            send("🔔 <b>В портфеле появились сделки</b>\n\n"
                 f"Сделок всего: {prev} → <b>{cur}</b> (+{cur - prev})\n"
                 f"Последняя дата сделки: {esc(str(meta.get('last_date')))}\n\n"
                 "⚠️ Параметры вытащить не удалось — посмотри лог этого запуска.")
            state["count"] = cur
            save_state(state_path, state)
            return 0, cur - prev, "details unavailable"
        now = int(time.time())
        if now - int(state.get("warn_at") or 0) > WARN_COOLDOWN:
            send("⚠️ <b>Мониторинг не видит сделок</b>\n\n"
                 f"browser={use_browser}, способ: {esc(how or '—')}\n"
                 f"Что пришло в ответе: {esc(diag)}\n\n"
                 "Частые причины: сайт закрылся от адресов дата-центра (капча), "
                 "Chromium не стартовал, или файл скрипта повреждён.")
            state["warn_at"] = now
            save_state(state_path, state)
        return 1, 0, "empty result"

    # --- случай 2: строки есть ---
    meta = parse_meta(html)
    name = meta.get("name") or portfolio
    rows = parse_rows(html)
    state["count"] = meta.get("count") or state.get("count")

    if not state.get("baseline"):
        state["seen"] = {row_key(r, name): 1 for r in rows}
        state["baseline"] = True
        save_state(state_path, state)
        send("✅ <b>Мониторинг запущен</b>\n\n"
             f"Портфель: <b>{esc(name)}</b>\n"
             f"Сделок всего: {esc(str(state.get('count')))}\n"
             f"Последняя дата сделки: {esc(str(meta.get('last_date')))}\n"
             f"Строк в выдаче: {len(rows)}, источник: {esc(how)}\n\n"
             "Дальше присылаю только новые покупки и продажи.")
        return 0, 0, ""

    fresh = [r for r in rows if row_key(r, name) not in state["seen"]]
    merged = dict(state["seen"])
    merged.update({row_key(r, name): 1 for r in rows})
    state["seen"] = {k: 1 for k in list(merged)[-MAX_SEEN:]}
    save_state(state_path, state)

    for r in list(reversed(fresh))[:MAX_PER_RUN]:
        send(message(r, name, url + "#transactions"))
    if len(fresh) > MAX_PER_RUN:
        send(f"…и ещё {len(fresh) - MAX_PER_RUN} новых сделок: {url}#transactions")
    log.info("строк %d, новых %d, источник %s", len(rows), len(fresh), how)
    return 0, len(fresh), ""


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

    code, new, err = run(token, chats, url, use_browser, state_path)
    print(f'RESULT new={new} error={json.dumps(err, ensure_ascii=False)}')
    return code


if __name__ == "__main__":
    sys.exit(main())
