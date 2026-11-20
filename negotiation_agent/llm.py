"""LLM 호출을 한 곳에 모아 둔 모듈. 다른 모델을 쓰려면 이 파일만 바꾸면 됩니다."""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

# 이 프로젝트의 .env (슈퍼바이저·대시보드가 다른 폴더에서 실행해도 같은 파일을 읽음)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PROVIDER = "anthropic"
MODEL = os.getenv("LLM_MODEL", "claude-sonnet-5-5")
MAX_TOKENS = 6000           # 초안 본문 + 한국어 번역 + 3번 사양 질문까지 들어가도 넉넉하게
RETRY_MAX_TOKENS = 12000    # 길이 제한에 걸려 잘리면 이 한도로 한 번 더


class Truncated(ValueError):
    """LLM 응답이 길이 제한(max_tokens)에 걸려 잘림."""


def _create(system: str, user: str, max_tokens: int):
    import anthropic

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )


def complete_full(system: str, user: str, max_tokens: int = MAX_TOKENS) -> tuple[str, str | None]:
    """(응답 글, stop_reason)."""
    resp = _create(system, user, max_tokens)
    return "".join(b.text for b in resp.content if b.type == "text"), getattr(resp, "stop_reason", None)


def complete(system: str, user: str, max_tokens: int = MAX_TOKENS) -> str:
    return complete_full(system, user, max_tokens)[0]


def complete_json(system: str, user: str, max_tokens: int = MAX_TOKENS) -> dict:
    """JSON 응답. 길이 제한에 걸려 잘리면 더 큰 한도로 한 번 다시 시도하고, 그래도 잘리면 Truncated."""
    text, stop = complete_full(system, user, max_tokens)
    if stop == "max_tokens":
        retry = max(RETRY_MAX_TOKENS, max_tokens * 2)
        print(f"  ⚠ LLM 응답이 길이 제한({max_tokens} 토큰)에 걸려 잘림 → {retry} 토큰으로 다시 시도", flush=True)
        text, stop = complete_full(system, user, retry)
        if stop == "max_tokens":
            raise Truncated(f"LLM 응답이 길이 제한({retry} 토큰)에 걸려 잘림 — 추가 요청을 줄이거나 다시 시도하세요")
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"LLM 응답이 JSON이 아닙니다: {text[:200]}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as err:
        raise ValueError(f"LLM 응답 JSON을 읽지 못했습니다 ({err.msg}): {text[:200]}") from err
