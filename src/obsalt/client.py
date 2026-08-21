"""HTTP client for the obsalt ingest & evidence API."""

from __future__ import annotations

from typing import Any, Self

import httpx

from obsalt.config import Settings
from obsalt.domain.enums import Provider, parse_provider


class ObsaltClient:
    """Talk to a running ``obsalt serve`` process.

    Org membership comes from the API key, not from the payload::

        client = ObsaltClient(api_key="secret")
        client.ingest("vapi", webhook_json)
        client.ingest_native(recorder.snapshot())
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        http_client: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(
            base_url=self.base_url, timeout=timeout, headers=headers
        )
        if api_key and http_client is not None:
            self._http.headers["X-API-Key"] = api_key

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, base_url: str | None = None
    ) -> ObsaltClient:
        settings = settings or Settings()
        keys = settings.parsed_api_keys()
        secret = next(iter(keys), None)
        host = "127.0.0.1" if settings.host in {"0.0.0.0", "::"} else settings.host
        url = base_url or f"http://{host}:{settings.port}"
        return cls(url, api_key=secret)

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def health(self) -> dict[str, Any]:
        response = self._http.get("/health")
        response.raise_for_status()
        return response.json()

    def ingest(self, provider: Provider | str, payload: dict[str, Any]) -> dict[str, Any]:
        slug = parse_provider(provider).value.replace("_", "-")
        if slug == "openai-realtime":
            path = "/v1/ingest/openai-realtime"
        else:
            path = f"/v1/ingest/{slug}"
        response = self._http.post(path, json=payload)
        response.raise_for_status()
        return response.json()

    def ingest_native(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.ingest(Provider.NATIVE, payload)

    def list_calls(self, *, agent_id: str | None = None) -> dict[str, Any]:
        params = {"agent_id": agent_id} if agent_id else None
        response = self._http.get("/v1/calls", params=params)
        response.raise_for_status()
        return response.json()

    def get_call(self, call_id: str) -> dict[str, Any]:
        response = self._http.get(f"/v1/calls/{call_id}")
        response.raise_for_status()
        return response.json()

    def search(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        response = self._http.post("/v1/search", json={"query": query, "limit": limit})
        response.raise_for_status()
        return response.json()

    def latency(self, *, agent_id: str | None = None) -> dict[str, Any]:
        params = {"agent_id": agent_id} if agent_id else None
        response = self._http.get("/v1/latency", params=params)
        response.raise_for_status()
        return response.json()

    def hangups(self) -> dict[str, Any]:
        response = self._http.get("/v1/hangups")
        response.raise_for_status()
        return response.json()

    def tools(self, *, agent_id: str | None = None) -> dict[str, Any]:
        params = {"agent_id": agent_id} if agent_id else None
        response = self._http.get("/v1/tools", params=params)
        response.raise_for_status()
        return response.json()
