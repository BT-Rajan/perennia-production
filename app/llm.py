"""
All outbound calls to LLM providers happen here, and only here. The API
key is read from the decrypted server-side config and attached to the
outbound request — it is never present in any response sent back to a
browser.
"""
import httpx

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class LLMError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _provider_defaults(provider: str, base_url: str) -> str:
    provider = (provider or "anthropic").lower()
    if provider == "deepseek":
        return "https://api.deepseek.com/v1"
    if provider == "openai":
        return "https://api.openai.com/v1"
    if provider == "anthropic":
        return "https://api.anthropic.com/v1"
    # custom / openai-compatible
    return base_url or "https://api.openai.com/v1"


async def chat_completion(
    *,
    provider: str,
    api_key: str,
    model: str,
    base_url: str,
    system_prompt: str,
    messages: list[dict],
    max_tokens: int = 1024,
) -> str:
    if not api_key:
        raise LLMError("API key not configured yet. Please contact the site administrator.", 503)

    provider = (provider or "anthropic").lower()

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            if provider == "anthropic":
                resp = await client.post(
                    f"{_provider_defaults(provider, base_url)}/messages",
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": model,
                        "max_tokens": max_tokens,
                        "system": system_prompt,
                        "messages": messages,
                    },
                )
                if resp.status_code >= 400:
                    raise LLMError(f"Anthropic API error ({resp.status_code}).", 502)
                try:
                    data = resp.json()
                except ValueError:
                    raise LLMError("Anthropic API returned an invalid (non-JSON) response.", 502)
                return "".join(b.get("text", "") for b in data.get("content", []))

            # DeepSeek / OpenAI / any OpenAI-compatible custom endpoint
            url = f"{_provider_defaults(provider, base_url)}/chat/completions"
            resp = await client.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json={
                    "model": model,
                    "messages": [{"role": "system", "content": system_prompt}, *messages],
                    "max_tokens": max_tokens,
                },
            )
            if resp.status_code >= 400:
                raise LLMError(f"{provider} API error ({resp.status_code}).", 502)
            try:
                data = resp.json()
            except ValueError:
                raise LLMError(f"{provider} API returned an invalid (non-JSON) response.", 502)
            try:
                return data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise LLMError(f"{provider} API returned an unexpected response shape.", 502)

        except httpx.RequestError as e:
            raise LLMError(f"Could not reach the {provider} API: {e}", 502)


async def test_connection(*, provider: str, api_key: str, model: str, base_url: str) -> tuple[bool, str]:
    """Minimal, cheap ping used by the admin panel's 'Test Connection' button."""
    if not api_key:
        return False, "No API key provided."
    try:
        reply = await chat_completion(
            provider=provider,
            api_key=api_key,
            model=model or "claude-haiku-4-5-20251001",
            base_url=base_url,
            system_prompt="Reply with the single word: ok",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=5,
        )
        return True, reply.strip() or "Connected."
    except LLMError as e:
        return False, str(e)
