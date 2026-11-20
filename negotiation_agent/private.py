"""내부 전용 협상 기준 (목표가·상한가). 공용 DB(trade.db)에는 넣지 않는다.

공용 DB는 슈퍼바이저 검토관(LLM)과 대시보드가 읽기 때문에, 상한가는 이 프로젝트의 로컬 DB에만 둔다.
  위치: negotiation_agent/data/private.db (git에 올리지 않음). 테스트에서는 환경변수 COMM_PRIVATE_DB로 바꿀 수 있다.
  키: 공용 DB의 products.id
"""
import os
import re
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS negotiation_settings (
    product_id        INTEGER PRIMARY KEY,   -- 공용 DB products.id
    target_price_usd  REAL,                  -- 역제안 기본값 (공급처에 말해도 되는 값)
    max_price_usd     REAL,                  -- 상한가: 어떤 초안에도, LLM 입력에도 넣지 않음
    preferred_payment_terms TEXT,
    updated_at        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);
"""


def path() -> Path:
    return Path(os.environ.get("COMM_PRIVATE_DB") or Path(__file__).resolve().parent.parent / "data" / "private.db")


def _connect() -> sqlite3.Connection:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def get(product_id: int) -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM negotiation_settings WHERE product_id = ?", (product_id,)).fetchone()
    return dict(row) if row else {}


def set_(product_id: int, target=None, max_price=None, payment=None) -> dict:
    cur = get(product_id)
    vals = {"target_price_usd": target if target is not None else cur.get("target_price_usd"),
            "max_price_usd": max_price if max_price is not None else cur.get("max_price_usd"),
            "preferred_payment_terms": payment if payment is not None else cur.get("preferred_payment_terms")}
    with _connect() as conn:
        conn.execute(
            "INSERT INTO negotiation_settings (product_id, target_price_usd, max_price_usd, preferred_payment_terms)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(product_id) DO UPDATE SET target_price_usd = excluded.target_price_usd,"
            " max_price_usd = excluded.max_price_usd, preferred_payment_terms = excluded.preferred_payment_terms,"
            " updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')",
            (product_id, *vals.values()))
    return get(product_id)


def all_rows() -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM negotiation_settings ORDER BY product_id")]


# 상한가와 같은 숫자라도 바로 뒤에 이런 단위가 오면 가격이 아니라 수량·기간·크기로 보고 넘어간다 (오탐 줄이기).
# 가격으로 읽힐 수 있는 숫자(통화 표시, 단위 없음)는 그대로 막는다 — 보수적으로.
NON_PRICE_UNIT = re.compile(
    r"\s*(?:(?:pcs|pc|pieces?|units?|sets?|pairs?|cartons?|ctns?|boxes|days?|weeks?|months?|years?|yrs?|hours?|hrs?|"
    r"percent|cm|mm|m|meters?|inch(?:es)?|in|kg|g|grams?|lbs?|ml|l|liters?|w|watts?|v|volts?|mah)\b|%|"
    r"개월|개|일|주|달|년|시간|세트|박스|장|켤레|센티|미리|"
    r"个|件|套|天|周|月|年|箱|只|双|小时|厘米|毫米|公斤|克)", re.I)


def _is_non_price(text, start, end):
    """숫자 text[start:end] 가 수량·기간·크기·목록 번호면 True. 앞에 통화 표시가 있으면 항상 가격으로 본다."""
    before = text[max(0, start - 6):start]
    if re.search(r"(\$|usd|us\$|rmb|cny|¥|￥|€|eur|달러|원|元)\s*$", before, re.I):
        return False
    after = text[end:end + 12]
    if re.match(r"\s*(\$|usd|dollars?|rmb|cny|元|美元|달러|불)", after, re.I):
        return False
    if NON_PRICE_UNIT.match(after):
        return True
    line_start = text.rfind("\n", 0, start) + 1      # 목록 번호: 줄 맨 앞의 "3. " / "3) "
    return bool(re.fullmatch(r"\s*", text[line_start:start]) and re.match(r"[.)]\s", text[end:end + 2]))


def leaks_max_price(text: str, product_id: int | None = None) -> bool:
    """글에 내부 상한가와 같은 숫자가 (가격으로 읽힐 수 있게) 있으면 True.

    product_id 가 None 이면 등록된 모든 품목의 상한가로 검사한다(보수적).
    '1,000' / '1000.00' / '3.5.' 같은 표기 차이는 숫자로 바꿔 비교한다.
    같은 숫자라도 '50 pcs'·'3 days'·'20%'·목록 번호 '3.' 처럼 가격이 아닌 게 분명하면 넘어간다 (통화 표시가 붙으면 막음).
    """
    rows = [get(product_id)] if product_id is not None else all_rows()
    limits = [r["max_price_usd"] for r in rows if r and r.get("max_price_usd")]
    if not limits or not text:
        return False
    for mt in re.finditer(r"\d[\d,]*\.?\d*", text):
        raw = mt.group()
        try:
            n = float(raw.replace(",", "").rstrip("."))
        except ValueError:
            continue
        if any(abs(n - mp) < 1e-9 for mp in limits):
            end = mt.end() - (1 if raw.endswith(".") else 0)
            if not _is_non_price(text, mt.start(), end):
                return True
    return False
