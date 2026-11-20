import pytest


@pytest.fixture(autouse=True)
def no_real_gmail(tmp_path, monkeypatch):
    """테스트가 실제 Gmail 토큰을 쓰지 않게 (로그인해 둔 PC에서도 auto 테스트가 Gmail에 접속하지 않음)."""
    monkeypatch.setenv("GMAIL_TOKEN", str(tmp_path / "no_gmail_token.json"))
    monkeypatch.setenv("GMAIL_CREDENTIALS", str(tmp_path / "no_gmail_credentials.json"))


@pytest.fixture(autouse=True)
def no_real_fx(monkeypatch):
    """테스트가 실제 환율 API에 접속하지 않게 (환율이 필요하면 FX_RATES 환경변수로)."""
    from negotiation_agent import hub

    def offline(*a, **k):
        raise OSError("테스트에서는 환율 API 접속 안 함")
    monkeypatch.delenv("FX_RATES", raising=False)
    monkeypatch.setattr(hub.urllib.request, "urlopen", offline)
    hub._fx_cache.clear()
