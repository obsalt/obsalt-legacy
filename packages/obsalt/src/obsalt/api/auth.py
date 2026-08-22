from __future__ import annotations

from fastapi import Header, HTTPException

from obsalt.runtime import ApiPrincipal, Runtime


def _runtime(request_runtime: Runtime | None = None) -> Runtime:
    if request_runtime is None:
        raise HTTPException(status_code=500, detail="runtime missing")
    return request_runtime


async def current_principal(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ApiPrincipal:
    # FastAPI dependency is bound in routes via a closure; this module helper
    # is used by require_scope through request.state.
    raise HTTPException(status_code=401, detail="unauthenticated")


def require_scope(scope: str):
    async def dep(
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ApiPrincipal:
        from fastapi import Request

        # populated in a real dependency below
        raise HTTPException(status_code=401, detail="unauthenticated")

    async def bound(
        request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ApiPrincipal:
        runtime: Runtime = request.app.state.runtime
        if not x_api_key:
            raise HTTPException(status_code=401, detail="missing API key")
        principal = runtime.authenticate_api_key(x_api_key)
        if principal is None:
            raise HTTPException(status_code=401, detail="invalid API key")
        order = {"ingest": 0, "read": 1, "analyze": 2, "admin": 3}
        if order.get(principal.scope, -1) < order.get(scope, 99) and principal.scope != "admin":
            if scope != principal.scope and not (principal.scope == "admin"):
                raise HTTPException(status_code=403, detail="insufficient scope")
        if scope != "ingest" and principal.scope == "ingest":
            raise HTTPException(status_code=403, detail="insufficient scope")
        if scope == "admin" and principal.scope != "admin":
            raise HTTPException(status_code=403, detail="insufficient scope")
        return principal

    return bound
