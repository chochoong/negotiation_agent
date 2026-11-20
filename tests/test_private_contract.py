"""4번 cost_agent 가 쓰는 약속: private.py 를 패키지 import 없이 파일로 읽어 get / set_(product_id, target, max_price) 호출.
이 시그니처·동작을 바꾸면 4번 자동 반영이 깨진다 (../cost_agent/cost_agent/hub.py load_private·apply_negotiation)."""
import importlib.util
from pathlib import Path

import negotiation_agent


def _load_like_cost_agent():
    f = Path(negotiation_agent.__file__).parent / "private.py"
    spec = importlib.util.spec_from_file_location("_negotiation_private", f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cost_agent_can_set_target_and_max_without_touching_payment(tmp_path, monkeypatch):
    monkeypatch.setenv("COMM_PRIVATE_DB", str(tmp_path / "private.db"))
    private = _load_like_cost_agent()
    private.set_(7, payment="T/T 30/70")                 # 사람이 넣은 결제 조건
    assert private.get(7)["target_price_usd"] is None
    private.set_(7, 2.8, 3.5)                             # 4번: 위치 인자 (목표가, 포기 가격=상한가)
    row = private.get(7)
    assert (row["target_price_usd"], row["max_price_usd"], row["preferred_payment_terms"]) == (2.8, 3.5, "T/T 30/70")
    assert private.leaks_max_price("we can pay 3.5 USD", 7)
