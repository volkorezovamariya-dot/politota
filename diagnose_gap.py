#!/usr/bin/env python3
"""
POLITOTA — диагностика провала в конце периода.

Прогон 2024-01-01 дал последнего кандидата 15 января 2026. От него до
26 июля 2026 — 192 дня без единого кандидата, при средней частоте примерно
1,4 кандидата в месяц на предыдущих 24 месяцах.

Две конкурирующие гипотезы:

  H_real     — BIS действительно не публиковал подходящих правил полгода.
               Тогда это резкий сдвиг режима, и он критически влияет на
               базовую ставку: любое окно в 2026 году получит нулевой исход.

  H_artifact — выгрузка обрывается. Тогда под вопросом ВСЯ выборка,
               а не только хвост.

Скрипт их разделяет. Ничего не пишет на диск, только печатает.
"""

import sys
import time
from collections import Counter

import requests

API = "https://www.federalregister.gov/api/v1"
GAP_START = "2026-01-01"
FIELDS = ["document_number", "title", "publication_date", "cfr_references", "type"]


def get(params):
    r = requests.get(f"{API}/documents.json", params=params, timeout=120)
    if r.status_code == 400:
        return []
    r.raise_for_status()
    return r.json().get("results", [])


def main():
    r = requests.get(f"{API}/agencies.json", timeout=60)
    r.raise_for_status()
    slug = next(
        a["slug"] for a in r.json()
        if "industry and security" in (a.get("name") or "").lower()
    )
    print(f"слаг BIS: {slug}\n")

    # Проверка 1. Есть ли У BIS вообще правила после GAP_START?
    docs = get(
        [("conditions[agencies][]", slug),
         ("conditions[type][]", "RULE"),
         ("conditions[publication_date][gte]", GAP_START),
         ("per_page", "1000"), ("order", "oldest")]
        + [("fields[]", f) for f in FIELDS]
    )
    print(f"[1] правил BIS с {GAP_START}: {len(docs)}")
    for d in docs:
        parts = {
            str(x.get("part")) for x in (d.get("cfr_references") or [])
            if str(x.get("title")) == "15"
        }
        mark = "744!" if "744" in parts else "    "
        print(f"    {mark} {d['publication_date']}  {d['title'][:78]}")

    if not docs:
        print("\n    ⚠ Ноль правил BIS за полгода — это само по себе аномально.")
        print("      Подозрение смещается на H_artifact: проверить фильтр по типу")
        print("      документа (может, действия ушли в PRORULE или NOTICE).")

    time.sleep(0.4)

    # Проверка 2. Контроль без фильтра по агентству — жив ли вообще API на этих датах.
    ctrl = get(
        [("conditions[type][]", "RULE"),
         ("conditions[publication_date][gte]", GAP_START),
         ("per_page", "5"), ("order", "newest"),
         ("fields[]", "publication_date"), ("fields[]", "title")]
    )
    print(f"\n[2] контроль: правил ЛЮБЫХ агентств с {GAP_START} — получено {len(ctrl)}")
    if ctrl:
        print(f"    свежайшее: {ctrl[0]['publication_date']}")
        print("    → API отдаёт свежие данные, дело не в индексации")

    time.sleep(0.4)

    # Проверка 3. Все типы документов BIS за провал — не переехали ли действия.
    allt = get(
        [("conditions[agencies][]", slug),
         ("conditions[publication_date][gte]", GAP_START),
         ("per_page", "1000"), ("order", "oldest"),
         ("fields[]", "type"), ("fields[]", "title"), ("fields[]", "publication_date")]
    )
    print(f"\n[3] документы BIS всех типов с {GAP_START}: {len(allt)}")
    for k, v in Counter(d.get("type") for d in allt).most_common():
        print(f"    {k}: {v}")

    print("\n" + "=" * 62)
    print("ЧТЕНИЕ РЕЗУЛЬТАТА")
    print("  [1] > 0 и есть строки с 744  → H_artifact: ломается наш фильтр")
    print("  [1] > 0, но 744 нигде нет    → H_real: BIS не трогал Entity List")
    print("  [1] = 0, а [3] > 0           → действия сменили тип документа")
    print("  [1] = 0 и [3] = 0, [2] > 0   → BIS молчал полгода. Проверить руками")
    print("  [2] = 0                      → проблема в API или в запросе, не в BIS")
    print("=" * 62)
    print("\nЛюбой исход фиксируется в docs/decisions/ — это первое")
    print("столкновение выборки с реальностью, и оно должно остаться в истории.")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        sys.exit(f"[СЕТЬ] {type(e).__name__}: {e}")
