"""
Shared LLM invocation helper for Phase 3.

Routing order respects settings.synthetic_query_llm:
  "local"      — transformers pipeline from settings.synthetic_query_local_model
  "ollama"     — HTTP POST to settings.ollama_url
  "anthropic"  — Anthropic API via settings.anthropic_api_key

Raises RuntimeError if the configured backend is unavailable.
"""
import logging
import urllib.request
import urllib.error
import json

from config.settings import settings

logger = logging.getLogger(__name__)


def call_llm(prompt: str, max_tokens: int = 512) -> str:
    """Call the configured LLM and return the response text."""
    backend = settings.synthetic_query_llm
    if backend == "local":
        return _call_local(prompt, max_tokens)
    elif backend == "ollama":
        return _call_ollama(prompt, max_tokens)
    elif backend == "anthropic":
        return _call_anthropic(prompt, max_tokens)
    else:
        raise RuntimeError(f"Unknown synthetic_query_llm backend: {backend!r}")


def _call_ollama(prompt: str, max_tokens: int) -> str:
    url = settings.ollama_url.rstrip("/") + "/api/generate"
    payload = json.dumps({
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": 0.7},
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
            return data.get("response", "").strip()
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Ollama unreachable at {settings.ollama_url}: {exc.reason}"
        ) from exc


def _call_anthropic(prompt: str, max_tokens: int) -> str:
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "synthetic_query_llm=anthropic but ANTHROPIC_API_KEY is not set in .env"
        )
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _call_local(prompt: str, max_tokens: int) -> str:
    model_path = settings.synthetic_query_local_model
    if not model_path:
        raise RuntimeError(
            "synthetic_query_llm=local but synthetic_query_local_model is not set"
        )
    import torch
    from transformers import pipeline as hf_pipeline

    if not hasattr(_call_local, "_pipe") or _call_local._model_path != model_path:
        logger.info("[llm] loading local model %s", model_path)
        device = 0 if torch.cuda.is_available() else -1
        _call_local._pipe = hf_pipeline(
            "text-generation", model=model_path,
            device=device, torch_dtype=torch.float16 if device >= 0 else torch.float32,
        )
        _call_local._model_path = model_path

    result = _call_local._pipe(
        prompt, max_new_tokens=max_tokens, do_sample=True, temperature=0.7,
        return_full_text=False,
    )
    return result[0]["generated_text"].strip()
