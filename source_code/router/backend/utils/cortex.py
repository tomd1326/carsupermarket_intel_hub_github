"""Ask AI model calls, shared by every page's Ask AI.

Switched off on 2026-10-30: the website must not use Snowflake Cortex (or any
Snowflake AI function), so it never spends Snowflake AI credits - only plain
queries. Ask AI comes back when the Claude API (Anthropic Console) key is set
up; until then every call raises AIUnavailable, which the pages show as a
message. Nothing here connects to Snowflake.
"""

AI_OFF_MESSAGE = "Ask AI is switched off until the Claude API is set up"


class AIUnavailable(RuntimeError):
    pass


def current_model() -> str:
    return ""


def cortex_complete(messages: list[dict], max_tokens: int, temperature: float) -> dict:
    raise AIUnavailable(AI_OFF_MESSAGE)
