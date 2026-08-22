from __future__ import annotations

from fastapi import Header, HTTPException, Request

from obsalt.runtime import ApiPrincipal, Runtime


def require_scope(scope: str):
    async def bound(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ApiPrincipal:
        runtime: Runtime = request.app.state.runtime
        if not x_api_key:
            raise HTTPException(status_code=401, detail="missing API key")
        principal = runtime.authenticate_api_key(x_api_key)
        if principal is None:
            raise HTTPException(status_code=401, detail="invalid API key")
        if scope == "admin" and principal.scope != "admin":
            raise HTTPException(status_code=403, detail="insufficient scope")
        if principal.scope == "ingest" and scope != "ingest":
            raise HTTPException(status_code=403, detail="insufficient scope")
        order = {"ingest": 0, "read": 1, "analyze": 2, "admin": 3}
        if order.get(principal.scope, -1) < order.get(scope, 99) and principal.scope != "admin":
            raise HTTPException(status_code=403, detail="insufficient scope")
        return principal

    return bound
