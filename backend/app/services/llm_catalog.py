"""
Model catalogues for the LLM providers that publish one.

A provider id does not narrow the choice enough to type a model from memory:
an aggregator like OpenRouter routes several hundred, the list changes every
week, and which of them a given account can actually reach is not knowable
from here. So the catalogue is fetched from the provider rather than pinned in
the registry, cached while it is still worth trusting, and allowed to fail -
the settings form falls back to a free-text model id, which is also the only
way to use a model newer or more private than any catalogue.
"""

from __future__ import annotations

import threading
import time

import requests
from loguru import logger

from app.config import config
from app.models.llm_provider import LLMProviderSpec, get_llm_provider


# The catalogue changes on the provider's release schedule, not ours. Half an
# hour keeps a settings screen opened repeatedly off the network without
# hiding a model added this morning for the rest of the session. The cache
# lives in the process, so restarting the app is always a way to force it.
_CACHE_TTL_SECONDS = 30 * 60

# Connect, then read. Someone is watching a form while this runs, so it has to
# give up soon enough to leave them typing in the fallback field instead.
_TIMEOUT_SECONDS = (5, 15)

_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


def _tls_verify() -> bool:
    """
    Mirror of the footage downloader's check, against the same config flag.

    Someone who had to turn verification off for a corporate proxy needs it
    off here too, or the model list is the one request that fails.
    """
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")
    return bool(tls_verify)


def _catalog_url(provider: LLMProviderSpec) -> str:
    """
    Where this provider lists its models.

    Built from the *configured* Base URL rather than the registry default, so
    someone pointing the provider at a gateway or a mirror gets that gateway's
    catalogue - a list of what it will actually route - instead of the
    upstream one it may not serve.
    """
    if not provider.model_catalog_path:
        return ""

    base_url = provider.resolve_base_url(
        config.app.get(provider.config_key("base_url"), "")
    )
    if not base_url:
        return ""
    return f"{base_url.rstrip('/')}/{provider.model_catalog_path.lstrip('/')}"


def _is_free(entry: dict) -> bool:
    """
    Whether a catalogue entry costs nothing to call.

    Read from the provider's own pricing rather than the ``:free`` suffix in
    the id: on OpenRouter today 18 ids carry the suffix while 22 entries are
    actually priced at zero, so the name is the less complete answer. A
    provider that publishes no pricing is not free - it is unknown, and
    guessing in the user's favour is how a "free models only" filter bills
    somebody.
    """
    pricing = entry.get("pricing")
    if not isinstance(pricing, dict):
        return False
    for field in ("prompt", "completion"):
        try:
            if float(pricing.get(field)) != 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _parse_models(payload) -> list[dict]:
    """
    Read an OpenAI-shaped ``/models`` listing.

    An entry needs an id and nothing else. The friendly name is decoration,
    and a provider that omits it still has to come out usable.
    """
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []

    models: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        label = str(entry.get("name") or "").strip()
        models.append(
            {
                "id": model_id,
                "label": label or model_id,
                "free": _is_free(entry),
            }
        )

    models.sort(key=lambda model: model["id"])
    return models


def _fetch(url: str, api_key: str) -> list[dict]:
    headers = {"Accept": "application/json"}
    # OpenRouter serves its catalogue unauthenticated, which is what puts the
    # list on screen before a key is entered. A private gateway may not, so
    # the key travels whenever there is one to send.
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    response = requests.get(
        url,
        headers=headers,
        proxies=config.proxy,
        verify=_tls_verify(),
        timeout=_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return _parse_models(response.json())


def list_models(provider_id: str) -> dict:
    """
    The models a provider publishes, and whatever went wrong getting them.

    Never raises. A catalogue that cannot be reached is no reason to keep
    anyone out of the settings screen: the model id is a text field
    underneath, and filling it in is a supported way to use every provider.

    Only successful fetches are cached, so a transient failure is retried on
    the next call rather than remembered for half an hour.
    """
    provider = get_llm_provider(provider_id)
    if provider is None:
        return {"models": [], "error": f"{provider_id}: unsupported llm provider"}

    url = _catalog_url(provider)
    if not url:
        return {"models": [], "error": None}

    with _cache_lock:
        cached = _cache.get(url)
    if cached and time.time() - cached["fetched_at"] < _CACHE_TTL_SECONDS:
        return {"models": cached["models"], "error": None}

    api_key = str(config.app.get(provider.config_key("api_key"), "") or "")
    try:
        models = _fetch(url, api_key)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        logger.warning(f"failed to read the {provider_id} model catalogue: {reason}")
        # A list that was real half an hour ago beats an empty dropdown, so an
        # expired entry is still served - with the error, so the form can say
        # the list may be out of date.
        if cached:
            return {"models": cached["models"], "error": reason}
        return {"models": [], "error": reason}

    with _cache_lock:
        _cache[url] = {"models": models, "fetched_at": time.time()}
    return {"models": models, "error": None}
