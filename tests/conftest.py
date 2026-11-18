import pytest


@pytest.fixture(autouse=True)
def no_real_gmail(tmp_path, monkeypatch):
    """테스트가 실제 Gmail 토큰을 쓰지 않게 (로그인해 둔 PC에서도 auto 테스트가 Gmail에 접속하지 않음)."""
    monkeypatch.setenv("GMAIL_TOKEN", str(tmp_path / "no_gmail_token.json"))
    monkeypatch.setenv("GMAIL_CREDENTIALS", str(tmp_path / "no_gmail_credentials.json"))
