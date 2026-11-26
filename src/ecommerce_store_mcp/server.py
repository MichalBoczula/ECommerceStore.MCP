from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

import httpx2
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from ecommerce_store_mcp.catalog import CatalogClient, PhoneDetails, PhoneList
from ecommerce_store_mcp.settings import Settings


def create_server(settings: Settings, *, transport: httpx2.AsyncBaseTransport | None = None) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[CatalogClient]:
        async with httpx2.AsyncClient(
            base_url=f"{str(settings.bff_base_url).rstrip('/')}/",
            timeout=httpx2.Timeout(60.0, connect=5.0),
            transport=transport,
        ) as client:
            yield CatalogClient(client)

    server = MCPServer("ECommerce Store", version="0.1.0", lifespan=lifespan)
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @server.custom_route("/health/live", methods=["GET"])
    async def live(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @server.tool(title="List mobile phones", annotations=read_only)
    async def list_mobile_phones(
        ctx: Context[CatalogClient, Request],
        amount: Annotated[int, Field(ge=1, le=50, description="Maximum number of phones to return.")] = 15,
    ) -> PhoneList:
        """List active phones from the store catalog with IDs, prices, and key specifications."""
        return await ctx.request_context.lifespan_context.list_phones(amount)

    @server.tool(title="Get mobile phone details", annotations=read_only)
    async def get_mobile_phone(
        ctx: Context[CatalogClient, Request],
        phone_id: Annotated[UUID, Field(description="Catalog ID returned by list_mobile_phones.")],
    ) -> PhoneDetails:
        """Read a phone's description, images, price, and technical details by its catalog ID."""
        return await ctx.request_context.lifespan_context.get_phone(phone_id)

    return server


def main() -> None:
    settings = Settings()
    create_server(settings).run(
        transport="streamable-http", host=settings.host, port=settings.port, stateless_http=True, json_response=True
    )
