#!/usr/bin/env python3
"""
POLITOTA — диагностика провала в конце периода.

Прогон 2024-01-01 дал последнего кандидата 15 января 2026. От него до
26 июля 2026 — 192 дня без единого кандидата, при средней частоте примерно
1,4 кандидата в месяц на предыдущих 24 месяцах.

Диагностика ничего не пишет на диск, только печатает.
"""

import sys
import time
from collections import Counter

import requests

API = "https://www.federalregister.gov/api/v1"
GAP_START = "2026-01-01"
FIELDS = [
    "document_number",
    "title",
    "publication_date",
    "cfr_references",
    "type",
    "raw_text_url",
]


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

    # [1] RULE BIS после начала разрыва.
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

    time.sleep(0.4)

    # [2] Контроль свежести API без фильтра по агентству.
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

    # [3] Все типы документов BIS.
    allt = get(
        [("conditions[agencies][]", slug),
         ("conditions[publication_date][gte]", GAP_START),
         ("per_page", "1000"), ("order", "oldest")]
        + [("fields[]", f) for f in FIELDS]
    )
    print(f"\n[3] документы BIS всех типов с {GAP_START}: {len(allt)}")
    for k, v in Counter(d.get("type") for d in allt).most_common():
        print(f"    {k}: {v}")

    # [4] Точечная проверка смены типа документа.
    relevant = []
    for d in allt:
        parts = {
            str(x.get("part")) for x in (d.get("cfr_references") or [])
            if str(x.get("title")) == "15"
        }
        title = d.get("title") or ""
        by_title = "entity list" in title.lower()
        by_cfr = "744" in parts
        if by_title or by_cfr:
            relevant.append((d, by_cfr, by_title))

    print(
        f"\n[4] документы BIS всех типов с CFR 744 и/или "
        f"'Entity List' в заголовке: {len(relevant)}"
    )
    for d, by_cfr, by_title in relevant:
        marks = ",".join(
            x for x, ok in (("744", by_cfr), ("TITLE", by_title)) if ok
        )
        print(
            f"    {d.get('type','?'):7} {d['publication_date']} "
            f"[{marks:9}] {d['title'][:90]}"
        )

    print("\n" + "=" * 62)
    print("ЧТЕНИЕ РЕЗУЛЬТАТА")
    print("  [4] содержит NOTICE после 2026-01-15 → действия могли сменить тип документа")
    print("  [4] после 2026-01-15 пуст            → свидетельство в пользу реального замедления")
    print("  [2] = 0                              → проблема в API или запросе")
    print("=" * 62)
    print("\nДо результата [4] решение в docs/decisions/ не фиксируется.")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        sys.exit(f"[СЕТЬ] {type(e).__name__}: {e}")
