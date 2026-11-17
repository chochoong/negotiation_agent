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


def leaks_max_price(text: str, product_id: int | None = None) -> bool:
    """글에 내부 상한가와 같은 숫자가 있으면 True.

    product_id 가 None 이면 등록된 모든 품목의 상한가로 검사한다(보수적).
    '1,000' / '1000.00' / '3.5.' 같은 표기 차이는 숫자로 바꿔 비교한다.
    """
    rows = [get(product_id)] if product_id is not None else all_rows()
    limits = [r["max_price_usd"] for r in rows if r and r.get("max_price_usd")]
    if not limits or not text:
        return False
    for raw in re.findall(r"\d[\d,]*\.?\d*", text):
        try:
            n = float(raw.replace(",", "").rstrip("."))
        except ValueError:
            continue
        if any(abs(n - mp) < 1e-9 for mp in limits):
            return True
    return False
