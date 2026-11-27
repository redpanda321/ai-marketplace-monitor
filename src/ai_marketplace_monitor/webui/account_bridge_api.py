import json
from typing import TYPE_CHECKING, Callable
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .account_bridge import MAX_BODY, BridgeError

if TYPE_CHECKING:
    from .hanggent_integration import HanggentIntegrationService


def register_bridge_routes(
    app: FastAPI, service: "HanggentIntegrationService", require_token: Callable[[Request], None]
) -> None:
    integration_auth = Depends(require_token)

    def call(operation: Callable[[], dict]) -> JSONResponse:
        try:
            return JSONResponse(operation(), headers={"Cache-Control": "no-store"})
        except BridgeError as error:
            raise HTTPException(409, str(error)) from None
        except ValueError:
            raise HTTPException(409, "account_busy") from None
        except Exception:
            raise HTTPException(503, "bridge_unavailable") from None

    async def body(request: Request) -> dict:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY:
                raise HTTPException(413, "bridge_body_too_large")
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError()
            return parsed
        except (ValueError, UnicodeError):
            raise HTTPException(422, "invalid_bridge_request") from None
        finally:
            raw.clear()

    @app.post("/api/integration/users/{owner}/account/bridge")
    def begin(owner: int, _: None = integration_auth) -> JSONResponse:
        if owner < 1:
            raise HTTPException(422, "invalid_bridge_request")
        return call(lambda: service.bridges.begin(owner))

    @app.get("/api/integration/users/{owner}/account/bridge/{bridge_id}")
    def status(owner: int, bridge_id: UUID, _: None = integration_auth) -> JSONResponse:
        return call(lambda: service.bridges.status(str(bridge_id), owner=owner))

    @app.delete("/api/integration/users/{owner}/account/bridge/{bridge_id}")
    def cancel(owner: int, bridge_id: UUID, _: None = integration_auth) -> JSONResponse:
        return call(lambda: service.bridges.cancel(str(bridge_id), owner))

    @app.post("/api/integration/account/bridge/{bridge_id}/{action}")
    async def capability_operation(
        bridge_id: UUID, action: str, request: Request, _: None = integration_auth
    ) -> JSONResponse:
        payload = await body(request)
        expected = (
            {"capability", "operation_id", "cookies"} if action == "complete" else {"capability"}
        )
        if action not in ("preview", "complete", "status") or set(payload) != expected:
            raise HTTPException(422, "invalid_bridge_request")
        capability = payload["capability"]
        if not isinstance(capability, str) or len(capability) != 43:
            raise HTTPException(422, "invalid_bridge_request")
        if action == "complete":
            return call(
                lambda: service.bridges.complete(
                    str(bridge_id), capability, payload["operation_id"], payload["cookies"]
                )
            )
        return call(lambda: service.bridges.status(str(bridge_id), capability=capability))
