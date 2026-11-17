"""LLM 호출을 한 곳에 모아 둔 모듈. 다른 모델을 쓰려면 이 파일만 바꾸면 됩니다."""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

# 이 프로젝트의 .env (슈퍼바이저·대시보드가 다른 폴더에서 실행해도 같은 파일을 읽음)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PROVIDER = "anthropic"
MODEL = os.getenv("LLM_MODEL", "claude-sonnet-5-5")


def complete(system: str, user: str, max_tokens: int = 3000) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def complete_json(system: str, user: str, max_tokens: int = 3000) -> dict:
    text = complete(system, user, max_tokens).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"LLM 응답이 JSON이 아닙니다: {text[:200]}")
    return json.loads(text[start:end + 1])
