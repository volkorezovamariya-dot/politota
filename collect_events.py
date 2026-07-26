#!/usr/bin/env python3
"""
POLITOTA — сбор исторической выборки событий класса `entity_list`.

Реализует раздел 13 протокола (PROTOCOL v0.4).

Два этапа, намеренно разделённые:
  Этап 1 (надёжный)   — выгрузка кандидатов из Federal Register и фильтрация
                        по формальным признакам. Здесь ошибок почти не бывает.
  Этап 2 (best-effort) — разбор текста правил. Ошибается систематически,
                        поэтому НЕ доверяется: каждая строка уходит в очередь
                        ручной проверки, см. допущение A4 протокола.

Скрипт не решает развилки раздела 12 — он их обнажает. Все восемь вынесены
в блок CONFIG. Пока они не проставлены осознанно, запускать бессмысленно.

Зависимости: requests
Сеть: требуется доступ к federalregister.gov (публичный API, ключ не нужен)
"""

import csv
import hashlib
import json
import platform
import random
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import requests

# ─────────────────────────────────────────────────────────────────────────────
# ARCHITECTURE — развилки раздела 12 протокола. ЗАМОРОЖЕНЫ после решения
# архитектора. Изменение любого значения = новая версия критерия разрешения
# и полная пересборка выборки (раздел 0). Значения ниже = рекомендации аудита.
# ─────────────────────────────────────────────────────────────────────────────

EVENT_FAMILY = "entity_list"        # Развилка 1. Только одно семейство.
HORIZON_DAYS = 30                    # Развилка 2. Используется на этапе сетки.
UNIT_OF_OBSERVATION = "rule"         # Развилка 3. Единица — правило, не организация.
INCLUDE_HK_MACAU = True              # Развилка 4.
TARGET_TYPE = "binary"               # Развилка 6. n_entities — атрибут, не цель.
REGIME_INTERACTIONS = False          # Развилка 7.
USE_COST_MATRIX = False              # Развилка 8.

# ─────────────────────────────────────────────────────────────────────────────
# EXPERIMENT — параметры прогона. НЕ заморожены. Меняются свободно,
# фиксируются в manifest.json. Смена значения здесь не является изменением
# архитектурного решения и не требует записи в docs/decisions/.
# ─────────────────────────────────────────────────────────────────────────────

HISTORY_START = "2014-01-01"         # Развилка 5. Для дымового прогона: "2024-01-01"
AGGREGATE_SAME_DAY = True            # Раздел 5. Второй прогон — со значением False
                                     # (обязательная проверка чувствительности).

# ─────────────────────────────────────────────────────────────────────────────
# RUN — техническое.
# ─────────────────────────────────────────────────────────────────────────────

RESOLUTION_VERSION = "0.4"
DATASET_VERSION = "0.1"

REVIEW_SAMPLE_SIZE = 30              # раздел 13, п. 8: не менее 20
REVIEW_SEED = 20260726               # раздел 16.2: seed фиксируется всегда

OUT_DIR = Path("data")
API = "https://www.federalregister.gov/api/v1"
PAUSE = 0.34                         # вежливость к публичному API

# Администрации США — для поля policy_regime (раздел 9.5).
REGIMES = [
    ("obama_2",  date(2013, 1, 20), date(2017, 1, 20)),
    ("trump_1",  date(2017, 1, 20), date(2021, 1, 20)),
    ("biden",    date(2021, 1, 20), date(2025, 1, 20)),
    ("trump_2",  date(2025, 1, 20), date(2100, 1, 1)),
]

CN_PATTERNS = [
    (r"\bchina\b", "China"),
    (r"\bhong\s*kong\b", "Hong Kong"),
    (r"\bmacau\b|\bmacao\b", "Macau"),
]

ADD_MARKERS = [r"\badding\b", r"\bis amended by adding\b", r"\bare added\b"]
REMOVE_MARKERS = [r"\bremoving\b", r"\bis amended by removing\b", r"\bare removed\b"]


def preflight():
    print("[-1] преполётная проверка связности…")
    r = requests.get(f"{API}/agencies.json", timeout=30)
    r.raise_for_status()
    n = len(r.json())
    if n < 10:
        raise SystemExit(
            f"API ответил, но вернул {n} агентств — это не похоже на норму. "
            "Проверить, не отдаёт ли прокси заглушку вместо ответа."
        )
    print(f"[-1] ок: API доступен, агентств в справочнике {n}")


def resolve_bis_slug():
    r = requests.get(f"{API}/agencies.json", timeout=60)
    r.raise_for_status()
    hits = [
        a for a in r.json()
        if "industry and security" in (a.get("name") or "").lower()
    ]
    if len(hits) != 1:
        print("Кандидаты на слаг BIS:", [(a["slug"], a["name"]) for a in hits])
        raise SystemExit(
            "Слаг BIS не определился однозначно. Выбрать вручную и вписать в код — "
            "но зафиксировать выбор в docs/decisions/."
        )
    print(f"[0] Слаг BIS: {hits[0]['slug']}")
    return hits[0]["slug"]


FIELDS = [
    "document_number", "title", "publication_date", "effective_on",
    "citation", "cfr_references", "raw_text_url", "correction_of",
    "public_inspection_pdf_url", "html_url", "type", "action",
]


def harvest(slug):
    docs, page = [], 1
    while True:
        params = [
            ("conditions[agencies][]", slug),
            ("conditions[type][]", "RULE"),
            ("conditions[publication_date][gte]", HISTORY_START),
            ("per_page", "1000"),
            ("page", str(page)),
            ("order", "oldest"),
        ] + [("fields[]", f) for f in FIELDS]

        r = requests.get(f"{API}/documents.json", params=params, timeout=120)
        if r.status_code == 400:
            break
        r.raise_for_status()
        batch = r.json().get("results", [])
        if not batch:
            break
        docs.extend(batch)
        print(f"[1] страница {page}: +{len(batch)} (всего {len(docs)})")
        page += 1
        time.sleep(PAUSE)
    return docs


def touches_744(doc):
    refs = doc.get("cfr_references") or []
    for ref in refs:
        if str(ref.get("title")) == "15" and str(ref.get("part")) == "744":
            return True, "cfr_field"
    if not refs:
        return True, "empty_refs_needs_text_check"
    return False, "cfr_field_excludes"


def fetch_text(url):
    if not url:
        return ""
    try:
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"    ! текст не получен: {e}")
        return ""


def inspect_text(txt):
    low = txt.lower()
    countries = sorted({
        name for pat, name in CN_PATTERNS
        if re.search(pat, low)
        and (INCLUDE_HK_MACAU or name == "China")
    })
    return {
        "mentions_744": bool(re.search(r"744", txt)),
        "mentions_supplement_4": bool(re.search(r"supplement\s+no\.?\s*4", low)),
        "has_add_marker": any(re.search(p, low) for p in ADD_MARKERS),
        "has_remove_marker": any(re.search(p, low) for p in REMOVE_MARKERS),
        "countries_mentioned": ";".join(countries),
        "n_entities_guess": len(re.findall(r"(?m)^\s*\(\d+\)\s+", txt)) or "",
        "text_sha256": hashlib.sha256(txt.encode("utf-8", "ignore")).hexdigest()[:16],
    }


def regime_for(d):
    for name, lo, hi in REGIMES:
        if lo <= d < hi:
            return name
    return "unknown"


def env_fingerprint():
    return {
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python": sys.version.split()[0],
        "requests": requests.__version__,
        "platform": platform.platform(),
        "review_seed": REVIEW_SEED,
    }


def write_review_sample(rows):
    if not rows:
        return []

    strata = defaultdict(list)
    for r in rows:
        strata[(r["policy_regime"], r["candidate_date"][:4])].append(r)

    rng = random.Random(REVIEW_SEED)
    sample, keys = [], sorted(strata)
    for k in keys:
        sample.append(rng.choice(strata[k]))

    remaining = [r for r in rows if r not in sample]
    rng.shuffle(remaining)
    sample += remaining[: max(0, REVIEW_SAMPLE_SIZE - len(sample))]

    path = OUT_DIR / "review_sample.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(sample)

    print(f"[✓] {path} — {len(sample)} строк из {len(keys)} страт")
    return sample


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = resolve_bis_slug()
    docs = harvest(slug)
    print(f"[1] всего правил BIS с {HISTORY_START}: {len(docs)}")

    rows = []
    for i, d in enumerate(docs, 1):
        keep, reason = touches_744(d)
        if not keep:
            continue
        if d.get("correction_of"):
            continue

        print(f"[2] {i}/{len(docs)} {d['document_number']} ({reason})")
        obs = inspect_text(fetch_text(d.get("raw_text_url")))
        time.sleep(PAUSE)

        if not obs["countries_mentioned"]:
            continue

        pub = datetime.strptime(d["publication_date"], "%Y-%m-%d").date()

        rows.append({
            "candidate_date": d["publication_date"],
            "event_family": EVENT_FAMILY,
            "policy_regime": regime_for(pub),
            "document_number": d["document_number"],
            "citation": d.get("citation") or "",
            "title": (d.get("title") or "")[:300],
            "effective_on": d.get("effective_on") or "",
            "had_public_inspection": bool(d.get("public_inspection_pdf_url")),
            "source_url": d.get("html_url") or "",
            "cfr_filter_reason": reason,
            **obs,
            "MANUAL_is_event": "",
            "MANUAL_event_date": "",
            "MANUAL_n_entities": "",
            "MANUAL_has_removals": "",
            "MANUAL_note": "",
        })

    rows.sort(key=lambda r: r["candidate_date"])
    path = OUT_DIR / "events_candidates.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["empty"])
        w.writeheader()
        w.writerows(rows)

    review = write_review_sample(rows)

    manifest = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "resolution_version": RESOLUTION_VERSION,
        "dataset_version": DATASET_VERSION,
        "environment": env_fingerprint(),
        "architecture": {
            "event_family": EVENT_FAMILY,
            "horizon_days": HORIZON_DAYS,
            "unit_of_observation": UNIT_OF_OBSERVATION,
            "include_hk_macau": INCLUDE_HK_MACAU,
            "target_type": TARGET_TYPE,
            "regime_interactions": REGIME_INTERACTIONS,
            "use_cost_matrix": USE_COST_MATRIX,
        },
        "experiment": {
            "history_start": HISTORY_START,
            "aggregate_same_day": AGGREGATE_SAME_DAY,
        },
        "n_rules_harvested": len(docs),
        "n_candidates": len(rows),
        "n_review_sample": len(review),
        "note": (
            "events_candidates.csv — НЕ выборка. Это очередь ручной проверки. "
            "data/events.csv создаётся только после заполнения колонок MANUAL_* "
            "и применения агрегации по дате."
        ),
    }
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n[✓] {path} — {len(rows)} кандидатов")
    print("[!] Это очередь проверки, а не выборка событий.")
    print("[!] Дальше: разметить вручную data/review_sample.csv (случайная,")
    print("    стратифицированная выборка), затем остальные MANUAL_*,")
    print("    затем агрегировать по дате → data/events.csv")


if __name__ == "__main__":
    try:
        preflight()
        main()
    except requests.HTTPError as e:
        sys.exit(f"[HTTP {e.response.status_code if e.response is not None else '?'}] {e}")
    except requests.RequestException as e:
        sys.exit(
            f"[СЕТЬ] {type(e).__name__}: {e}\n"
            "Federal Register API публичный и ключа не требует — значит дело "
            "в исходящем доступе рантайма, а не в контракте API.\n"
            "Проверить: DNS до www.federalregister.gov, исходящий HTTPS, прокси.\n"
            "Ни один файл не создан, фиктивных данных нет."
        )
    except KeyboardInterrupt:
        sys.exit("\nПрервано. Частичные файлы не записаны.")
