"""Data demo service: CRUD operations over Postgres with good and N+1 regression variants."""

import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, status
from psycopg import Error as PsycopgError
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from services._common import (
    FaultManager,
    create_pool,
    get_logger,
    ping_db,
    setup_fault_middleware,
    setup_fault_routes,
    setup_health_routes,
    setup_metrics,
)

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
DATA_SERVICE_VARIANT = os.getenv("DATA_SERVICE_VARIANT", "good").strip().lower()

db_pool: AsyncConnectionPool | None = None
fault_manager = FaultManager()

# In-memory item store for tests and offline runs
IN_MEMORY_ITEMS: list[dict[str, Any]] = [
    {
        "id": 1,
        "name": "Standard Widget",
        "description": "General purpose widget",
        "details": [{"key": "color", "value": "blue"}, {"key": "weight", "value": "1kg"}],
    },
    {
        "id": 2,
        "name": "Premium Widget",
        "description": "High performance widget",
        "details": [{"key": "color", "value": "gold"}, {"key": "weight", "value": "2kg"}],
    },
]


class ItemCreate(BaseModel):
    """Payload for creating a new catalogue item."""

    name: str
    description: str
    details: list[dict[str, str]] = []


async def init_db(pool: AsyncConnectionPool) -> None:
    """Initialize schema and seed initial items if table is empty."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS item_details (
                id SERIAL PRIMARY KEY,
                item_id INT REFERENCES items(id) ON DELETE CASCADE,
                detail_key TEXT NOT NULL,
                detail_value TEXT NOT NULL
            );
            """
        )
        await cur.execute("SELECT COUNT(*) FROM items")
        row = await cur.fetchone()
        if row and row[0] == 0:
            for item in IN_MEMORY_ITEMS:
                await cur.execute(
                    "INSERT INTO items (id, name, description) VALUES (%s, %s, %s)",
                    (item["id"], item["name"], item["description"]),
                )
                for detail in item.get("details", []):
                    await cur.execute(
                        "INSERT INTO item_details (item_id, detail_key, detail_value) "
                        "VALUES (%s, %s, %s)",
                        (item["id"], detail["key"], detail["value"]),
                    )
        await conn.commit()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage database pool lifecycle and table initialization."""
    global db_pool
    if DATABASE_URL:
        db_pool = create_pool(DATABASE_URL)
        await db_pool.open()
        fault_manager.pool = db_pool
        try:
            await init_db(db_pool)
        except PsycopgError as exc:
            logger.error("data_service_db_init_failed", error=str(exc))
    try:
        yield
    finally:
        if db_pool:
            await db_pool.close()


app = FastAPI(title="data-service", lifespan=lifespan)
setup_metrics(app, "data-service")
setup_fault_middleware(app, fault_manager)
setup_fault_routes(app, fault_manager)


async def check_db_readiness() -> bool:
    """Readiness probe for database connection."""
    if db_pool is None:
        return True
    return await ping_db(db_pool)


setup_health_routes(app, [check_db_readiness])


@app.get("/items", tags=["Items"])
async def get_items() -> dict[str, Any]:
    """Return catalogue items via single join (:good) or N+1 queries (:regression)."""
    if db_pool is None:
        return {"items": IN_MEMORY_ITEMS, "variant": DATA_SERVICE_VARIANT}

    async with db_pool.connection() as conn, conn.cursor() as cur:
        if DATA_SERVICE_VARIANT == "regression":
            # Genuine N+1 Query Regression:
            # 1 initial query to get item list
            await cur.execute("SELECT id, name, description FROM items ORDER BY id")
            rows = await cur.fetchall()
            items: list[dict[str, Any]] = []
            for row in rows:
                item_id, name, desc = row[0], row[1], row[2]
                # + N individual queries over the wire for details
                await cur.execute(
                    "SELECT detail_key, detail_value FROM item_details WHERE item_id = %s",
                    (item_id,),
                )
                detail_rows = await cur.fetchall()
                items.append(
                    {
                        "id": item_id,
                        "name": name,
                        "description": desc,
                        "details": [{"key": d[0], "value": d[1]} for d in detail_rows],
                    }
                )
            return {"items": items, "variant": "regression"}

        # :good variant: Single consolidated query with JSON aggregation
        await cur.execute(
            """
                SELECT i.id, i.name, i.description,
                       COALESCE(
                           json_agg(json_build_object('key', d.detail_key, 'value', d.detail_value))
                           FILTER (WHERE d.id IS NOT NULL),
                           '[]'::json
                       ) AS details
                FROM items i
                LEFT JOIN item_details d ON i.id = d.item_id
                GROUP BY i.id, i.name, i.description
                ORDER BY i.id;
                """
        )
        rows = await cur.fetchall()
        items = []
        for row in rows:
            details = row[3] if isinstance(row[3], list) else json.loads(row[3] or "[]")
            items.append(
                {
                    "id": row[0],
                    "name": row[1],
                    "description": row[2],
                    "details": details,
                }
            )
        return {"items": items, "variant": "good"}


@app.post("/items", tags=["Items"], status_code=status.HTTP_201_CREATED)
async def create_item(payload: ItemCreate) -> dict[str, Any]:
    """Insert a new catalogue item and its details."""
    if db_pool is None:
        new_id = len(IN_MEMORY_ITEMS) + 1
        item_dict = {
            "id": new_id,
            "name": payload.name,
            "description": payload.description,
            "details": payload.details,
        }
        IN_MEMORY_ITEMS.append(item_dict)
        return item_dict

    async with db_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO items (name, description) VALUES (%s, %s) RETURNING id",
            (payload.name, payload.description),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=500, detail="Failed to insert item")
        item_id = row[0]

        for d in payload.details:
            await cur.execute(
                "INSERT INTO item_details (item_id, detail_key, detail_value) VALUES (%s, %s, %s)",
                (item_id, d.get("key", ""), d.get("value", "")),
            )
        await conn.commit()

        return {
            "id": item_id,
            "name": payload.name,
            "description": payload.description,
            "details": payload.details,
        }
