from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .config import settings
from .models import SubscriptionCreate
from .service import service

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

app = FastAPI(title="Proxy Pool", version="0.1.0")
templates = Jinja2Templates(directory="/app/templates")


@app.on_event("startup")
async def startup() -> None:
    await service.startup()


@app.on_event("shutdown")
async def shutdown() -> None:
    await service.shutdown()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "status": service.status(),
            "subscriptions": service.list_subscriptions(),
            "nodes": service.list_nodes(),
        },
    )


@app.get("/api/status")
async def api_status() -> dict:
    return service.status()


@app.get("/api/subscriptions")
async def api_subscriptions() -> list[dict]:
    return [item.model_dump() for item in service.list_subscriptions()]


@app.post("/api/subscriptions")
async def api_add_subscription(payload: SubscriptionCreate) -> dict:
    try:
        record = await service.add_subscription(payload)
        return {"success": True, "subscription": record.model_dump()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/subscriptions/{subscription_id}/refresh")
async def api_refresh_subscription(subscription_id: str) -> dict:
    try:
        record = await service.refresh_subscription(subscription_id)
        return {"success": True, "subscription": record.model_dump()}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/subscriptions/{subscription_id}")
async def api_delete_subscription(subscription_id: str) -> dict:
    service.delete_subscription(subscription_id)
    return {"success": True}


@app.get("/api/nodes")
async def api_nodes() -> list[dict]:
    return [item.model_dump() for item in service.list_nodes()]


@app.post("/api/check")
async def api_check() -> dict:
    state = await service.check_bound_nodes()
    return {"success": True, "bound_nodes": len([n for n in state.nodes if n.bound_port])}


@app.post("/api/rebuild")
async def api_rebuild() -> dict:
    state = service.rebuild_pool()
    return {"success": True, "bound_nodes": len([n for n in state.nodes if n.bound_port])}


@app.get("/api/proxy/next")
async def api_next_proxy(
    strategy: str | None = Query(default=None),
    require_healthy: bool = Query(default=True),
) -> dict:
    try:
        lease = await service.next_proxy(strategy=strategy, require_healthy=require_healthy)
        return {"success": True, **lease.model_dump()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
