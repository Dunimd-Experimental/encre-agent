#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre Agent.
# The Encre Agent project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# DISCLAIMER: Users must comply with applicable AI regulations.
# Non-compliance may result in service termination or legal liability.

from __future__ import annotations

"""Module: builtin/database.py

Database tool supporting SQLite, PostgreSQL, MySQL, SQL Server, Oracle,
DuckDB, ClickHouse, MongoDB, and Redis.  Provides full CRUD, schema
introspection, indexing, query plan, size stats, export, and database-
specific metadata.
"""
import asyncio
import csv
import io
import json
import os
import re
import sqlite3
import urllib.parse
from enum import Enum
from typing import Any, Optional

from encre.tools.base import build_tool
from encre.tools._encoding import decode_bytes


class DBType(Enum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    MSSQL = "mssql"
    ORACLE = "oracle"
    DUCKDB = "duckdb"
    CLICKHOUSE = "clickhouse"
    MONGODB = "mongodb"
    REDIS = "redis"


_pools: dict[str, Any] = {}
_pool_locks: dict[str, asyncio.Lock] = {}
_default_pool_max = 20


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

def _normalize_path(path: str) -> str:
    if path.startswith("/"):
        stripped = path.lstrip("/")
        if stripped == ":memory:" or not stripped:
            return stripped
        if os.name == "nt" and len(stripped) > 1 and stripped[1] == ":":
            return stripped
    return path


def _parse_db_url(url: str) -> tuple[DBType, dict[str, Any]]:
    if not url or url.strip() == "":
        return DBType.SQLITE, {"database": ":memory:"}
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme in ("sqlite", "sqlite3"):
        raw_path = parsed.path
        if parsed.netloc:
            raw_path = parsed.netloc + raw_path
        raw_path = _normalize_path(raw_path)
        if not raw_path or raw_path == ":memory:":
            raw_path = ":memory:"
        return DBType.SQLITE, {"database": raw_path}

    elif scheme in ("postgresql", "postgres", "postgresql+asyncpg"):
        return DBType.POSTGRESQL, {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 5432,
            "user": parsed.username or "",
            "password": parsed.password or "",
            "database": parsed.path.lstrip("/") or "postgres",
        }

    elif scheme in ("mysql", "mysql+aiomysql", "mariadb", "mysql+asyncmy"):
        return DBType.MYSQL, {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 3306,
            "user": parsed.username or "",
            "password": parsed.password or "",
            "database": parsed.path.lstrip("/") or "mysql",
        }

    elif scheme in ("mssql", "sqlserver", "mssql+pymssql", "mssql+aioodbc"):
        return DBType.MSSQL, {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 1433,
            "user": parsed.username or "",
            "password": parsed.password or "",
            "database": parsed.path.lstrip("/") or "master",
        }

    elif scheme in ("oracle", "oracle+oracledb", "oracle+cx_oracle"):
        return DBType.ORACLE, {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 1521,
            "user": parsed.username or "",
            "password": parsed.password or "",
            "service_name": parsed.path.lstrip("/") or "XE",
        }

    elif scheme in ("duckdb",):
        raw_path = parsed.path
        if parsed.netloc:
            raw_path = parsed.netloc + raw_path
        raw_path = _normalize_path(raw_path)
        if not raw_path or raw_path == ":memory:":
            raw_path = ":memory:"
        return DBType.DUCKDB, {"database": raw_path}

    elif scheme in ("clickhouse", "clickhouse+native"):
        return DBType.CLICKHOUSE, {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 9000,
            "user": parsed.username or "default",
            "password": parsed.password or "",
            "database": parsed.path.lstrip("/") or "default",
        }

    elif scheme in ("mongodb", "mongodb+srv"):
        return DBType.MONGODB, {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 27017,
            "user": parsed.username or "",
            "password": parsed.password or "",
            "database": parsed.path.lstrip("/") or "test",
            "srv": scheme == "mongodb+srv",
        }

    elif scheme in ("redis", "rediss"):
        return DBType.REDIS, {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 6379,
            "user": parsed.username or "",
            "password": parsed.password or "",
            "db": int(parsed.path.lstrip("/") or "0"),
            "ssl": scheme == "rediss",
        }

    else:
        raise ValueError(
            f"Unsupported database URL scheme: '{scheme}'. "
            f"Supported: sqlite://, postgresql://, mysql://, mssql://, "
            f"oracle://, duckdb://, clickhouse://, mongodb://, redis://"
        )


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

def _is_read_only_query(sql: str) -> bool:
    cleaned = sql.strip().upper()
    if cleaned.startswith("SELECT") or cleaned.startswith("PRAGMA") or cleaned.startswith("EXPLAIN"):
        return True
    if cleaned.startswith("WITH"):
        return True
    if cleaned.startswith("SHOW") or cleaned.startswith("DESCRIBE") or cleaned.startswith("DESC"):
        return True
    return False


def _is_dangerous_ddl(sql: str) -> bool:
    cleaned = sql.strip().upper()
    dangerous = [
        r"^\s*DROP\s+(DATABASE|SCHEMA|TABLE|INDEX|VIEW|TRIGGER|FUNCTION|PROCEDURE|EXTENSION|PUBLICATION|SUBSCRIPTION)",
        r"^\s*TRUNCATE\s+",
        r"^\s*ALTER\s+(DATABASE|SCHEMA|SYSTEM|SERVER|LOGIN|USER|ROLE)",
        r"^\s*CREATE\s+(DATABASE|SCHEMA|USER|ROLE|LOGIN)",
        r"^\s*REINDEX\s+(DATABASE|SCHEMA|SYSTEM)",
        r"^\s*GRANT\s+|^\s*REVOKE\s+",
        r"^\s*SHUTDOWN",
        r"^\s*ALTER\s+SYSTEM",
        r"^\s*DROP\s+OWNED",
        r"^\s*REASSIGN\s+OWNED",
    ]
    for pattern in dangerous:
        if re.match(pattern, cleaned):
            return True
    return False


# ---------------------------------------------------------------------------
# Pool management
# ---------------------------------------------------------------------------

async def _close_pool(db_url: str) -> str:
    if db_url not in _pools:
        return json.dumps({"status": "not_found", "message": f"No pool found for {db_url}"})
    pool = _pools.pop(db_url, None)
    _pool_locks.pop(db_url, None)
    if pool is None:
        return json.dumps({"status": "ok", "message": "No active pool"})
    try:
        db_type, _ = _parse_db_url(db_url)
        if db_type == DBType.POSTGRESQL:
            await pool.close()
        elif db_type == DBType.MYSQL:
            pool.close()
            await pool.wait_closed()
        elif db_type == DBType.MONGODB:
            pool.close()
        elif db_type == DBType.REDIS:
            await pool.aclose()
        elif db_type == DBType.SQLITE:
            if hasattr(pool, "close"):
                await pool.close()
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
    return json.dumps({"status": "ok", "message": f"Pool closed for {db_url}"})


async def _close_all_pools() -> str:
    results = []
    for url in list(_pools.keys()):
        r = await _close_pool(url)
        results.append(json.loads(r))
    return json.dumps({"status": "ok", "closed": len(results), "details": results})


async def _get_pool(db_url: str) -> Any:
    if db_url in _pools:
        return _pools[db_url]
    if db_url not in _pool_locks:
        _pool_locks[db_url] = asyncio.Lock()
    async with _pool_locks[db_url]:
        if db_url in _pools:
            return _pools[db_url]
        db_type, params = _parse_db_url(db_url)

        if db_type == DBType.SQLITE:
            try:
                import aiosqlite
                conn = await aiosqlite.connect(params["database"])
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA foreign_keys=ON")
                conn.row_factory = aiosqlite.Row
                _pools[db_url] = conn
                return conn
            except ImportError:
                _pools[db_url] = params["database"]
                return params["database"]

        elif db_type == DBType.POSTGRESQL:
            try:
                import asyncpg
                pool = await asyncpg.create_pool(
                    host=params["host"], port=params["port"],
                    user=params["user"], password=params["password"],
                    database=params["database"],
                    min_size=2, max_size=_default_pool_max,
                )
                _pools[db_url] = pool
                return pool
            except ImportError:
                raise ImportError("PostgreSQL: pip install asyncpg")

        elif db_type == DBType.MYSQL:
            try:
                import aiomysql
                pool = await aiomysql.create_pool(
                    host=params["host"], port=params["port"],
                    user=params["user"], password=params["password"],
                    db=params["database"],
                    minsize=1, maxsize=_default_pool_max, autocommit=False,
                )
                _pools[db_url] = pool
                return pool
            except ImportError:
                raise ImportError("MySQL: pip install aiomysql")

        elif db_type == DBType.MSSQL:
            _pools[db_url] = params
            return params

        elif db_type == DBType.ORACLE:
            _pools[db_url] = params
            return params

        elif db_type == DBType.DUCKDB:
            _pools[db_url] = params
            return params

        elif db_type == DBType.CLICKHOUSE:
            _pools[db_url] = params
            return params

        elif db_type == DBType.MONGODB:
            try:
                from motor.motor_asyncio import AsyncIOMotorClient
                if params["srv"]:
                    uri = f"mongodb+srv://{params['user']}:{params['password']}@{params['host']}/{params['database']}"
                elif params["user"]:
                    uri = f"mongodb://{params['user']}:{params['password']}@{params['host']}:{params['port']}/{params['database']}"
                else:
                    uri = f"mongodb://{params['host']}:{params['port']}/{params['database']}"
                client = AsyncIOMotorClient(uri)
                _pools[db_url] = client
                return client
            except ImportError:
                raise ImportError("MongoDB: pip install motor")

        elif db_type == DBType.REDIS:
            try:
                import redis.asyncio as aioredis
                kw = {
                    "host": params["host"], "port": params["port"],
                    "db": params["db"], "decode_responses": True,
                }
                if params["password"]:
                    kw["password"] = params["password"]
                if params["ssl"]:
                    kw["ssl"] = True
                client = aioredis.Redis(**kw)
                _pools[db_url] = client
                return client
            except ImportError:
                raise ImportError("Redis: pip install redis")


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

async def _execute_sqlite(pool, sql: str, params: Optional[list], limit: int, timeout: int) -> str:
    try:
        import aiosqlite
        use_async = isinstance(pool, aiosqlite.Connection)
    except ImportError:
        use_async = False
    if use_async:
        return await _sqlite_async(pool, sql, params, limit, timeout)
    else:
        return await _sqlite_sync(pool, sql, params, limit, timeout)


async def _sqlite_async(conn, sql: str, params: Optional[list], limit: int, timeout: int) -> str:
    try:
        cursor = await asyncio.wait_for(conn.execute(sql, params or []), timeout=timeout)
        sql_upper = sql.strip().upper()
        if sql_upper.startswith("SELECT") or sql_upper.startswith("PRAGMA") or sql_upper.startswith("EXPLAIN"):
            rows = await cursor.fetchmany(limit)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            results = [dict(zip(columns, row, strict=False)) for row in rows]
            await conn.commit()
            out = {"columns": columns, "rows": results, "count": len(results)}
            if len(results) == limit:
                out["truncated"] = True
            return json.dumps(out, indent=2, default=str)
        else:
            await conn.commit()
            out = {"affected_rows": cursor.rowcount}
            if cursor.lastrowid:
                out["last_insert_id"] = cursor.lastrowid
            return json.dumps(out)
    except asyncio.TimeoutError:
        return f"Error: Query timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _sqlite_sync(db_url, sql: str, params: Optional[list], limit: int, timeout: int) -> str:
    try:
        loop = asyncio.get_running_loop()

        def _exec_full() -> str:
            conn = sqlite3.connect(db_url)
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.cursor()
                cursor.execute(sql, params or [])
                sql_upper = sql.strip().upper()
                if sql_upper.startswith("SELECT") or sql_upper.startswith("PRAGMA") or sql_upper.startswith("EXPLAIN"):
                    rows = cursor.fetchmany(limit)
                    columns = [desc[0] for desc in cursor.description] if cursor.description else []
                    results = [dict(zip(columns, row, strict=False)) for row in rows]
                    conn.commit()
                    out = {"columns": columns, "rows": results, "count": len(results)}
                    if len(results) == limit:
                        out["truncated"] = True
                    return json.dumps(out, indent=2, default=str)
                else:
                    conn.commit()
                    out = {"affected_rows": cursor.rowcount}
                    if cursor.lastrowid:
                        out["last_insert_id"] = cursor.lastrowid
                    return json.dumps(out)
            finally:
                conn.close()

        try:
            return await asyncio.wait_for(loop.run_in_executor(None, _exec_full), timeout=timeout)
        except asyncio.TimeoutError:
            return f"Error: Query timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------

async def _execute_postgres(pool, sql: str, params: Optional[list], limit: int, timeout: int) -> str:
    try:
        async with pool.acquire() as conn:
            sql_upper = sql.strip().upper()
            is_query = sql_upper.startswith("SELECT") or sql_upper.startswith("WITH") or sql_upper.startswith("EXPLAIN")
            if is_query:
                rows = await asyncio.wait_for(
                    conn.fetch(sql, *params) if params else conn.fetch(sql),
                    timeout=timeout,
                )
                if rows:
                    columns = list(rows[0].keys())
                    all_rows = [dict(row) for row in rows]
                    truncated = False
                    if limit > 0 and len(all_rows) > limit:
                        all_rows = all_rows[:limit]
                        truncated = True
                    out = {"columns": columns, "rows": all_rows, "count": len(all_rows)}
                    if truncated:
                        out["truncated"] = True
                    return json.dumps(out, indent=2, default=str)
                return json.dumps({"columns": [], "rows": [], "count": 0})
            else:
                r = await asyncio.wait_for(
                    conn.execute(sql, *params) if params else conn.execute(sql),
                    timeout=timeout,
                )
                affected = 0
                if r:
                    try:
                        affected = int(r.split()[-1])
                    except (ValueError, TypeError):
                        affected = 0
                return json.dumps({"affected_rows": affected})
    except asyncio.TimeoutError:
        return f"Error: Query timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# MySQL
# ---------------------------------------------------------------------------

async def _execute_mysql(pool, sql: str, params: Optional[list], limit: int, timeout: int) -> str:
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                sql_upper = sql.strip().upper()
                is_query = sql_upper.startswith("SELECT") or sql_upper.startswith("SHOW") or sql_upper.startswith("DESCRIBE") or sql_upper.startswith("EXPLAIN") or sql_upper.startswith("WITH")
                await asyncio.wait_for(
                    cursor.execute(sql, params) if params else cursor.execute(sql),
                    timeout=timeout,
                )
                if is_query:
                    rows = await cursor.fetchmany(limit)
                    columns = [desc[0] for desc in cursor.description] if cursor.description else []
                    results = [dict(zip(columns, row, strict=False)) for row in rows]
                    out = {"columns": columns, "rows": results, "count": len(results)}
                    if len(results) == limit:
                        out["truncated"] = True
                    return json.dumps(out, indent=2, default=str)
                else:
                    await conn.commit()
                    out = {"affected_rows": cursor.rowcount}
                    if cursor.lastrowid:
                        out["last_insert_id"] = cursor.lastrowid
                    return json.dumps(out)
    except asyncio.TimeoutError:
        return f"Error: Query timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# SQL Server (pymssql, sync)
# ---------------------------------------------------------------------------

async def _execute_mssql(params_dict: dict, sql: str, params: Optional[list], limit: int, timeout: int) -> str:
    import pymssql

    def _query() -> str:
        conn = pymssql.connect(
            server=params_dict["host"], port=params_dict["port"],
            user=params_dict["user"], password=params_dict["password"],
            database=params_dict["database"],
        )
        try:
            with conn.cursor(as_dict=True) as cursor:
                cursor.execute(sql, params or [])
                sql_upper = sql.strip().upper()
                if sql_upper.startswith("SELECT") or sql_upper.startswith("WITH") or sql_upper.startswith("EXEC") or sql_upper.startswith("EXECUTE"):
                    all_rows = cursor.fetchmany(limit)
                    columns = list(all_rows[0].keys()) if all_rows else []
                    out = {"columns": columns, "rows": all_rows, "count": len(all_rows)}
                    if len(all_rows) == limit:
                        out["truncated"] = True
                    conn.commit()
                    return json.dumps(out, indent=2, default=str)
                else:
                    conn.commit()
                    return json.dumps({"affected_rows": cursor.rowcount})
        finally:
            conn.close()

    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _query), timeout=timeout)
    except asyncio.TimeoutError:
        return f"Error: Query timed out after {timeout}s"
    except Exception as e:
        return json.dumps({"error": f"MSSQL: {e}"})


# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------

async def _execute_oracle(params_dict: dict, sql: str, params: Optional[list], limit: int, timeout: int) -> str:
    import oracledb

    dsn = oracledb.makedsn(params_dict["host"], params_dict["port"], service_name=params_dict["service_name"])

    def _query() -> str:
        conn = oracledb.connect(user=params_dict["user"], password=params_dict["password"], dsn=dsn)
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, params or [])
                sql_upper = sql.strip().upper()
                if sql_upper.startswith("SELECT") or sql_upper.startswith("WITH") or sql_upper.startswith("EXPLAIN"):
                    all_rows = cursor.fetchmany(limit)
                    columns = [desc[0] for desc in cursor.description] if cursor.description else []
                    rows_dict = [dict(zip(columns, row, strict=False)) for row in all_rows]
                    out = {"columns": columns, "rows": rows_dict, "count": len(rows_dict)}
                    if len(rows_dict) == limit:
                        out["truncated"] = True
                    conn.commit()
                    return json.dumps(out, indent=2, default=str)
                else:
                    conn.commit()
                    return json.dumps({"affected_rows": cursor.rowcount})
        finally:
            conn.close()

    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _query), timeout=timeout)
    except asyncio.TimeoutError:
        return f"Error: Query timed out after {timeout}s"
    except Exception as e:
        return json.dumps({"error": f"Oracle: {e}"})


# ---------------------------------------------------------------------------
# DuckDB
# ---------------------------------------------------------------------------

async def _execute_duckdb(params_dict: dict, sql: str, params: Optional[list], limit: int, timeout: int) -> str:
    import duckdb

    def _query() -> str:
        conn = duckdb.connect(params_dict["database"])
        try:
            result = conn.execute(sql, params or [])
            sql_upper = sql.strip().upper()
            if sql_upper.startswith("SELECT") or sql_upper.startswith("WITH") or sql_upper.startswith("EXPLAIN") or sql_upper.startswith("SHOW") or sql_upper.startswith("DESCRIBE") or sql_upper.startswith("PRAGMA"):
                all_rows = result.fetchmany(limit)
                columns = [desc[0] for desc in result.description] if result.description else []
                rows_dict = [dict(zip(columns, row, strict=False)) for row in all_rows]
                out = {"columns": columns, "rows": rows_dict, "count": len(rows_dict)}
                if len(rows_dict) == limit:
                    out["truncated"] = True
                return json.dumps(out, indent=2, default=str)
            else:
                return json.dumps({"affected_rows": result.rowcount})
        finally:
            conn.close()

    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _query), timeout=timeout)
    except asyncio.TimeoutError:
        return f"Error: Query timed out after {timeout}s"
    except Exception as e:
        return json.dumps({"error": f"DuckDB: {e}"})


# ---------------------------------------------------------------------------
# ClickHouse
# ---------------------------------------------------------------------------

async def _execute_clickhouse(params_dict: dict, sql: str, params: Optional[list], limit: int, timeout: int) -> str:
    from clickhouse_driver import Client as CHClient

    def _query() -> str:
        client = CHClient(
            host=params_dict["host"], port=params_dict["port"],
            user=params_dict["user"], password=params_dict["password"],
            database=params_dict["database"],
        )
        try:
            sql_upper = sql.strip().upper()
            is_query = sql_upper.startswith("SELECT") or sql_upper.startswith("SHOW") or sql_upper.startswith("DESCRIBE") or sql_upper.startswith("EXPLAIN") or sql_upper.startswith("WITH")
            if is_query:
                rows = client.execute(sql, params or [], with_column_types=True)
                columns = [col[0] for col in rows[1]] if rows[1] else []
                data = rows[0]
                all_rows = [dict(zip(columns, row, strict=False)) for row in data]
                truncated = False
                if limit > 0 and len(all_rows) > limit:
                    all_rows = all_rows[:limit]
                    truncated = True
                out = {"columns": columns, "rows": all_rows, "count": len(all_rows)}
                if truncated:
                    out["truncated"] = True
                return json.dumps(out, indent=2, default=str)
            else:
                client.execute(sql, params or [])
                return json.dumps({"status": "ok"})
        finally:
            client.disconnect()

    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _query), timeout=timeout)
    except asyncio.TimeoutError:
        return f"Error: Query timed out after {timeout}s"
    except Exception as e:
        return json.dumps({"error": f"ClickHouse: {e}"})


# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------

async def _mongodb_find_one(client, db_name: str, collection: str, filter_doc: dict, timeout: int) -> str:
    try:
        db = client[db_name]
        doc = await asyncio.wait_for(db[collection].find_one(filter_doc), timeout=timeout)
        if doc:
            doc["_id"] = str(doc["_id"])
            return json.dumps({"document": doc}, indent=2, default=str)
        return json.dumps({"document": None})
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_find(client, db_name: str, collection: str, filter_doc: dict, limit: int, timeout: int,
                        sort: Optional[list] = None, projection: Optional[dict] = None,
                        skip: int = 0) -> str:
    try:
        db = client[db_name]
        col = db[collection]
        cursor = col.find(filter_doc, projection)
        if sort:
            cursor = cursor.sort(sort)
        if skip:
            cursor = cursor.skip(skip)
        cursor = cursor.limit(limit)
        rows = await asyncio.wait_for(cursor.to_list(length=limit), timeout=timeout)
        for r in rows:
            r["_id"] = str(r["_id"])
        return json.dumps({"count": len(rows), "documents": rows}, indent=2, default=str)
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_insert(client, db_name: str, collection: str, document: Any, timeout: int) -> str:
    try:
        db = client[db_name]
        col = db[collection]
        if isinstance(document, list):
            result = await asyncio.wait_for(col.insert_many(document), timeout=timeout)
            return json.dumps({"inserted_ids": [str(i) for i in result.inserted_ids], "count": len(result.inserted_ids)})
        else:
            result = await asyncio.wait_for(col.insert_one(document), timeout=timeout)
            return json.dumps({"inserted_id": str(result.inserted_id), "count": 1})
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_update(client, db_name: str, collection: str, filter_doc: dict, update_doc: dict, multi: bool, timeout: int) -> str:
    try:
        db = client[db_name]
        col = db[collection]
        if multi:
            result = await asyncio.wait_for(col.update_many(filter_doc, update_doc), timeout=timeout)
        else:
            result = await asyncio.wait_for(col.update_one(filter_doc, update_doc), timeout=timeout)
        return json.dumps({"matched_count": result.matched_count, "modified_count": result.modified_count, "upserted_id": str(result.upserted_id) if result.upserted_id else None})
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_delete(client, db_name: str, collection: str, filter_doc: dict, multi: bool, timeout: int) -> str:
    try:
        db = client[db_name]
        col = db[collection]
        if multi:
            result = await asyncio.wait_for(col.delete_many(filter_doc), timeout=timeout)
        else:
            result = await asyncio.wait_for(col.delete_one(filter_doc), timeout=timeout)
        return json.dumps({"deleted_count": result.deleted_count})
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_aggregate(client, db_name: str, collection: str, pipeline: list, limit: int, timeout: int) -> str:
    try:
        db = client[db_name]
        col = db[collection]
        cursor = col.aggregate(pipeline)
        rows = await asyncio.wait_for(cursor.to_list(length=limit), timeout=timeout)
        for r in rows:
            if "_id" in r:
                r["_id"] = str(r["_id"])
        return json.dumps({"count": len(rows), "documents": rows}, indent=2, default=str)
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_find_one_and_update(client, db_name: str, collection: str, filter_doc: dict,
                                        update_doc: dict, return_doc: str, timeout: int) -> str:
    try:
        db = client[db_name]
        col = db[collection]
        after = return_doc == "after"
        doc = await asyncio.wait_for(
            col.find_one_and_update(filter_doc, update_doc, return_document=after),
            timeout=timeout,
        )
        if doc:
            doc["_id"] = str(doc["_id"])
        return json.dumps({"document": doc}, indent=2, default=str)
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_find_one_and_delete(client, db_name: str, collection: str, filter_doc: dict, timeout: int) -> str:
    try:
        db = client[db_name]
        col = db[collection]
        doc = await asyncio.wait_for(col.find_one_and_delete(filter_doc), timeout=timeout)
        if doc:
            doc["_id"] = str(doc["_id"])
        return json.dumps({"document": doc}, indent=2, default=str)
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_find_one_and_replace(client, db_name: str, collection: str, filter_doc: dict,
                                         replacement: dict, return_doc: str, timeout: int) -> str:
    try:
        db = client[db_name]
        col = db[collection]
        after = return_doc == "after"
        doc = await asyncio.wait_for(
            col.find_one_and_replace(filter_doc, replacement, return_document=after),
            timeout=timeout,
        )
        if doc:
            doc["_id"] = str(doc["_id"])
        return json.dumps({"document": doc}, indent=2, default=str)
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_collections(client, db_name: str, timeout: int) -> str:
    try:
        db = client[db_name]
        names = await asyncio.wait_for(db.list_collection_names(), timeout=timeout)
        return json.dumps({"collections": names, "count": len(names)}, indent=2)
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_create_collection(client, db_name: str, collection: str, timeout: int) -> str:
    try:
        db = client[db_name]
        await asyncio.wait_for(db.create_collection(collection), timeout=timeout)
        return json.dumps({"status": "ok", "collection": collection})
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_drop_collection(client, db_name: str, collection: str, timeout: int) -> str:
    try:
        db = client[db_name]
        await asyncio.wait_for(db[collection].drop(), timeout=timeout)
        return json.dumps({"status": "ok", "dropped": collection})
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_rename_collection(client, db_name: str, old_name: str, new_name: str, timeout: int) -> str:
    try:
        db = client[db_name]
        await asyncio.wait_for(db[old_name].rename(new_name), timeout=timeout)
        return json.dumps({"status": "ok", "from": old_name, "to": new_name})
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_count(client, db_name: str, collection: str, filter_doc: dict, timeout: int) -> str:
    try:
        db = client[db_name]
        col = db[collection]
        count = await asyncio.wait_for(col.count_documents(filter_doc), timeout=timeout)
        return json.dumps({"count": count})
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_estimated_count(client, db_name: str, collection: str, timeout: int) -> str:
    try:
        db = client[db_name]
        col = db[collection]
        count = await asyncio.wait_for(col.estimated_document_count(), timeout=timeout)
        return json.dumps({"estimated_count": count})
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_distinct(client, db_name: str, collection: str, field: str, filter_doc: dict, timeout: int) -> str:
    try:
        db = client[db_name]
        col = db[collection]
        values = await asyncio.wait_for(col.distinct(field, filter_doc), timeout=timeout)
        return json.dumps({"field": field, "values": values, "count": len(values)}, indent=2, default=str)
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_indexes(client, db_name: str, collection: str, timeout: int) -> str:
    try:
        db = client[db_name]
        col = db[collection]
        indexes = await asyncio.wait_for(col.index_information(), timeout=timeout)
        result = []
        for name, info in indexes.items():
            result.append({"name": name, "key": info["key"], "unique": info.get("unique", False), "sparse": info.get("sparse", False)})
        return json.dumps({"indexes": result, "count": len(result)}, indent=2)
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_create_index(client, db_name: str, collection: str, keys: list, index_name: Optional[str],
                                 unique: bool, timeout: int) -> str:
    try:
        db = client[db_name]
        col = db[collection]
        kw = {"keys": keys}
        if index_name:
            kw["name"] = index_name
        if unique:
            kw["unique"] = True
        result = await asyncio.wait_for(col.create_index(**kw), timeout=timeout)
        return json.dumps({"status": "ok", "index_name": result})
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_drop_index(client, db_name: str, collection: str, index_name: str, timeout: int) -> str:
    try:
        db = client[db_name]
        col = db[collection]
        await asyncio.wait_for(col.drop_index(index_name), timeout=timeout)
        return json.dumps({"status": "ok", "dropped_index": index_name})
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_bulk_write(client, db_name: str, collection: str, operations: list, timeout: int) -> str:
    try:
        from pymongo import InsertOne, UpdateOne, DeleteOne, ReplaceOne
        db = client[db_name]
        col = db[collection]
        requests = []
        for op in operations:
            op_type = op.get("op")
            if op_type == "insert":
                requests.append(InsertOne(op["document"]))
            elif op_type == "update":
                requests.append(UpdateOne(op["filter"], op["update"], upsert=op.get("upsert", False)))
            elif op_type == "delete":
                requests.append(DeleteOne(op["filter"]))
            elif op_type == "replace":
                requests.append(ReplaceOne(op["filter"], op["replacement"], upsert=op.get("upsert", False)))
        result = await asyncio.wait_for(col.bulk_write(requests), timeout=timeout)
        return json.dumps({
            "inserted_count": result.inserted_count,
            "matched_count": result.matched_count,
            "modified_count": result.modified_count,
            "deleted_count": result.deleted_count,
            "upserted_count": result.upserted_count,
        })
    except asyncio.TimeoutError:
        return f"Error: Operation timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def _mongodb_watch(client, db_name: str, collection: str, pipeline: Optional[list], timeout: int) -> str:
    try:
        db = client[db_name]
        col = db[collection]
        kw = {}
        if pipeline:
            kw["pipeline"] = pipeline
        async with col.watch(**kw) as stream:
            change = await asyncio.wait_for(stream.try_next(), timeout=timeout)
            if change:
                if "_id" in change:
                    change["_id"] = str(change["_id"])
                return json.dumps({"change": change}, indent=2, default=str)
            return json.dumps({"note": "No change event within timeout"})
    except asyncio.TimeoutError:
        return json.dumps({"note": "No change event within timeout"})
    except Exception as e:
        return f"Error: {e}"


async def _handle_mongodb(pool, kwargs: dict, timeout: int) -> str:
    action = kwargs.get("mongodb_action", "find")
    db_name = kwargs.get("mongodb_db", "")
    collection = kwargs.get("mongodb_collection", "")
    limit = kwargs.get("limit", 100)

    if not db_name:
        _, params = _parse_db_url(kwargs.get("database_url", ""))
        db_name = params.get("database", "test")

    needs_collection = {"find", "find_one", "count", "estimated_count", "distinct", "update",
                        "delete", "insert", "aggregate", "indexes", "create_index", "drop_index",
                        "find_one_and_update", "find_one_and_delete", "find_one_and_replace",
                        "bulk_write", "watch", "create_collection", "drop_collection", "rename_collection"}
    if action in needs_collection and not collection and action not in ("collections", "create_collection", "drop_collection", "rename_collection"):
        return json.dumps({"error": "mongodb_collection is required"})

    try:
        if action == "find":
            filter_doc = _parse_json(kwargs.get("mongodb_filter", {}))
            sort = _parse_json(kwargs.get("mongodb_sort", None))
            projection = _parse_json(kwargs.get("mongodb_projection", None))
            skip = kwargs.get("mongodb_skip", 0)
            return await _mongodb_find(pool, db_name, collection, filter_doc, limit, timeout, sort, projection, skip)

        elif action == "find_one":
            filter_doc = _parse_json(kwargs.get("mongodb_filter", {}))
            return await _mongodb_find_one(pool, db_name, collection, filter_doc, timeout)

        elif action == "find_one_and_update":
            filter_doc = _parse_json(kwargs.get("mongodb_filter", {}))
            update_doc = _parse_json(kwargs.get("mongodb_update", {}))
            return_doc = kwargs.get("mongodb_return_document", "after")
            return await _mongodb_find_one_and_update(pool, db_name, collection, filter_doc, update_doc, return_doc, timeout)

        elif action == "find_one_and_delete":
            filter_doc = _parse_json(kwargs.get("mongodb_filter", {}))
            return await _mongodb_find_one_and_delete(pool, db_name, collection, filter_doc, timeout)

        elif action == "find_one_and_replace":
            filter_doc = _parse_json(kwargs.get("mongodb_filter", {}))
            replacement = _parse_json(kwargs.get("mongodb_replacement", {}))
            return_doc = kwargs.get("mongodb_return_document", "after")
            return await _mongodb_find_one_and_replace(pool, db_name, collection, filter_doc, replacement, return_doc, timeout)

        elif action == "insert":
            doc = _parse_json(kwargs.get("mongodb_document", {}))
            return await _mongodb_insert(pool, db_name, collection, doc, timeout)

        elif action == "update":
            filter_doc = _parse_json(kwargs.get("mongodb_filter", {}))
            update_doc = _parse_json(kwargs.get("mongodb_update", {}))
            multi = kwargs.get("mongodb_multi", False)
            return await _mongodb_update(pool, db_name, collection, filter_doc, update_doc, multi, timeout)

        elif action == "delete":
            filter_doc = _parse_json(kwargs.get("mongodb_filter", {}))
            multi = kwargs.get("mongodb_multi", True)
            return await _mongodb_delete(pool, db_name, collection, filter_doc, multi, timeout)

        elif action == "aggregate":
            pipeline = _parse_json(kwargs.get("mongodb_pipeline", []))
            return await _mongodb_aggregate(pool, db_name, collection, pipeline, limit, timeout)

        elif action == "collections":
            return await _mongodb_collections(pool, db_name, timeout)

        elif action == "create_collection":
            return await _mongodb_create_collection(pool, db_name, collection, timeout)

        elif action == "drop_collection":
            return await _mongodb_drop_collection(pool, db_name, collection, timeout)

        elif action == "rename_collection":
            new_name = kwargs.get("mongodb_new_name", "")
            if not new_name:
                return json.dumps({"error": "mongodb_new_name is required"})
            return await _mongodb_rename_collection(pool, db_name, collection, new_name, timeout)

        elif action == "count":
            filter_doc = _parse_json(kwargs.get("mongodb_filter", {}))
            return await _mongodb_count(pool, db_name, collection, filter_doc, timeout)

        elif action == "estimated_count":
            return await _mongodb_estimated_count(pool, db_name, collection, timeout)

        elif action == "distinct":
            field = kwargs.get("mongodb_field", "")
            filter_doc = _parse_json(kwargs.get("mongodb_filter", {}))
            return await _mongodb_distinct(pool, db_name, collection, field, filter_doc, timeout)

        elif action == "indexes":
            return await _mongodb_indexes(pool, db_name, collection, timeout)

        elif action == "create_index":
            keys = _parse_json(kwargs.get("mongodb_keys", []))
            index_name = kwargs.get("mongodb_index_name", None)
            unique = kwargs.get("mongodb_unique", False)
            return await _mongodb_create_index(pool, db_name, collection, keys, index_name, unique, timeout)

        elif action == "drop_index":
            index_name = kwargs.get("mongodb_index_name", "")
            if not index_name:
                return json.dumps({"error": "mongodb_index_name is required"})
            return await _mongodb_drop_index(pool, db_name, collection, index_name, timeout)

        elif action == "bulk_write":
            operations = _parse_json(kwargs.get("mongodb_operations", []))
            return await _mongodb_bulk_write(pool, db_name, collection, operations, timeout)

        elif action == "watch":
            pipeline = _parse_json(kwargs.get("mongodb_pipeline", None))
            return await _mongodb_watch(pool, db_name, collection, pipeline, timeout)

        else:
            return json.dumps({"error": f"Unknown MongoDB action: {action}"})
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"})


def _parse_json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

async def _handle_redis(pool, kwargs: dict, timeout: int) -> str:
    command = kwargs.get("redis_command", "").strip().upper()
    args = kwargs.get("redis_args", [])

    if not command:
        return json.dumps({"error": "redis_command is required"})

    try:
        method = getattr(pool, command.lower(), None)
        if method is None:
            return json.dumps({"error": f"Unknown Redis command: {command}"})
        result = await asyncio.wait_for(method(*args), timeout=timeout)
        return json.dumps({"command": command, "result": _serialize_redis_result(result)}, indent=2, default=str)
    except asyncio.TimeoutError:
        return f"Error: Redis command timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


def _serialize_redis_result(result: Any) -> Any:
    if isinstance(result, (list, tuple)):
        return [_serialize_redis_result(r) for r in result]
    if isinstance(result, bytes):
        return decode_bytes(result)
    if isinstance(result, dict):
        return {decode_bytes(k) if isinstance(k, bytes) else k: _serialize_redis_result(v) for k, v in result.items()}
    if isinstance(result, set):
        return [_serialize_redis_result(r) for r in result]
    return result


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------

async def _list_tables(db_url: str, db_type: DBType, pool) -> str:
    sql_map = {
        DBType.SQLITE: "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        DBType.POSTGRESQL: "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY tablename",
        DBType.MYSQL: "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME",
        DBType.MSSQL: "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME",
        DBType.ORACLE: "SELECT TABLE_NAME FROM ALL_TABLES WHERE OWNER = USER ORDER BY TABLE_NAME",
        DBType.DUCKDB: "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY table_name",
        DBType.CLICKHOUSE: "SHOW TABLES",
    }
    if db_type in sql_map:
        return await _execute_with_pool(db_url, db_type, pool, sql_map[db_type], None, 500, 30)
    if db_type == DBType.MONGODB:
        return await _mongodb_collections(pool, _db_name(db_url), 30)
    if db_type == DBType.REDIS:
        return json.dumps({"note": "Redis does not have tables. Use redis_command=KEYS or SCAN."})
    return json.dumps({"error": "Unsupported database type"})


async def _list_views(db_url: str, db_type: DBType, pool) -> str:
    sql_map = {
        DBType.SQLITE: "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name",
        DBType.POSTGRESQL: "SELECT schemaname||'.'||viewname AS view_name FROM pg_catalog.pg_views WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY view_name",
        DBType.MYSQL: "SELECT TABLE_NAME FROM information_schema.VIEWS WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME",
        DBType.MSSQL: "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.VIEWS ORDER BY TABLE_NAME",
        DBType.ORACLE: "SELECT VIEW_NAME FROM ALL_VIEWS WHERE OWNER = USER ORDER BY VIEW_NAME",
        DBType.DUCKDB: "SELECT table_name FROM information_schema.views WHERE table_schema = 'main' ORDER BY table_name",
        DBType.CLICKHOUSE: "SHOW VIEWS",
    }
    if db_type in sql_map:
        return await _execute_with_pool(db_url, db_type, pool, sql_map[db_type], None, 500, 30)
    if db_type == DBType.MONGODB:
        return json.dumps({"note": "MongoDB does not have views. Use aggregation pipelines instead."})
    if db_type == DBType.REDIS:
        return json.dumps({"note": "Redis does not have views."})
    return json.dumps({"error": "Unsupported database type"})


async def _list_indexes(db_url: str, db_type: DBType, pool, table: str = "") -> str:
    if db_type == DBType.SQLITE:
        if table:
            return await _execute_with_pool(db_url, db_type, pool, f"PRAGMA index_list({_quote_ident(table)})", None, 500, 30)
        return await _execute_with_pool(db_url, db_type, pool, "SELECT name FROM sqlite_master WHERE type='index' AND sql NOT NULL ORDER BY name", None, 500, 30)
    elif db_type == DBType.POSTGRESQL:
        if table:
            return await _execute_with_pool(db_url, db_type, pool, """
                SELECT indexname, indexdef FROM pg_indexes WHERE schemaname NOT IN ('pg_catalog','information_schema')
                AND tablename = $1 ORDER BY indexname
            """, [table], 500, 30)
        return await _execute_with_pool(db_url, db_type, pool, """
            SELECT schemaname||'.'||tablename AS table_name, indexname, indexdef
            FROM pg_indexes WHERE schemaname NOT IN ('pg_catalog','information_schema') ORDER BY tablename, indexname
        """, None, 500, 30)
    elif db_type == DBType.MYSQL:
        if table:
            return await _execute_with_pool(db_url, db_type, pool, f"SHOW INDEX FROM {_quote_ident(table)}", None, 500, 30)
        return json.dumps({"note": "Use the 'indexes' action with a 'table' parameter for MySQL, or query information_schema.STATISTICS."})
    elif db_type == DBType.MSSQL:
        if table:
            return await _execute_with_pool(db_url, db_type, pool, "SELECT i.name AS index_name, i.type_desc, ic.key_ordinal, c.name AS column_name FROM sys.indexes i JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id WHERE i.object_id = OBJECT_ID(%s) ORDER BY i.name, ic.key_ordinal", [table], 500, 30)
        return json.dumps({"note": "Use the 'indexes' action with a 'table' parameter for MSSQL."})
    elif db_type == DBType.ORACLE:
        if table:
            return await _execute_with_pool(db_url, db_type, pool, "SELECT index_name, index_type, uniqueness FROM all_indexes WHERE table_name = :1 ORDER BY index_name", [table.upper()], 500, 30)
        return json.dumps({"note": "Use the 'indexes' action with a 'table' parameter for Oracle."})
    elif db_type == DBType.DUCKDB:
        if table:
            return await _execute_with_pool(db_url, db_type, pool, "SELECT index_name, is_unique, is_primary, sql FROM duckdb_indexes() WHERE table_name = ? ORDER BY index_name", [table], 500, 30)
        return json.dumps({"note": "Use the 'indexes' action with a 'table' parameter for DuckDB."})
    elif db_type == DBType.CLICKHOUSE:
        return json.dumps({"note": "ClickHouse does not use traditional indexes. Use primary key / sorting key instead."})
    elif db_type == DBType.MONGODB:
        if table:
            return await _mongodb_indexes(pool, _db_name(db_url), table, 30)
        return json.dumps({"note": "Use the 'indexes' action with mongodb_collection parameter for MongoDB."})
    return json.dumps({"error": "Unsupported database type"})


async def _describe_table(db_url: str, db_type: DBType, pool, table: str) -> str:
    if db_type == DBType.SQLITE:
        return await _execute_with_pool(db_url, db_type, pool, f"PRAGMA table_info({_quote_ident(table)})", None, 500, 30)
    elif db_type == DBType.POSTGRESQL:
        return await _execute_with_pool(db_url, db_type, pool, """
            SELECT a.attname AS column_name,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                   CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END AS is_nullable,
                   COALESCE(pg_catalog.pg_get_expr(ad.adbin, ad.adrelid), '') AS default_value,
                   CASE WHEN a.attidentity = 'a' THEN 'YES' ELSE 'NO' END AS is_identity
            FROM pg_catalog.pg_attribute a
            LEFT JOIN pg_catalog.pg_attrdef ad ON a.attrelid = ad.adrelid AND a.attnum = ad.adnum
            WHERE a.attrelid = $1::regclass AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum
        """, [table], 500, 30)
    elif db_type == DBType.MYSQL:
        return await _execute_with_pool(db_url, db_type, pool, f"DESCRIBE {_quote_ident(table)}", None, 500, 30)
    elif db_type == DBType.MSSQL:
        return await _execute_with_pool(db_url, db_type, pool, """
            SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE, COLUMN_DEFAULT
            FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = %s ORDER BY ORDINAL_POSITION
        """, [table], 500, 30)
    elif db_type == DBType.ORACLE:
        return await _execute_with_pool(db_url, db_type, pool, """
            SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, NULLABLE, DATA_DEFAULT
            FROM ALL_TAB_COLUMNS WHERE TABLE_NAME = :1 ORDER BY COLUMN_ID
        """, [table.upper()], 500, 30)
    elif db_type == DBType.DUCKDB:
        return await _execute_with_pool(db_url, db_type, pool, f"DESCRIBE {_quote_ident(table)}", None, 500, 30)
    elif db_type == DBType.CLICKHOUSE:
        return await _execute_with_pool(db_url, db_type, pool, f"DESCRIBE TABLE {_quote_ident(table)}", None, 500, 30)
    elif db_type == DBType.MONGODB:
        return json.dumps({"note": "MongoDB is schemaless. Use mongodb_action=find_one to sample a document."})
    elif db_type == DBType.REDIS:
        return json.dumps({"note": "Redis does not have tables. Use redis_command=TYPE to check key types."})
    return json.dumps({"error": "Unsupported database type"})


async def _get_size(db_url: str, db_type: DBType, pool, table: str = "") -> str:
    if db_type == DBType.SQLITE:
        if table:
            return await _execute_with_pool(db_url, db_type, pool, f"SELECT count(*) AS row_count FROM {_quote_ident(table)}", None, 10, 30)
        return json.dumps({"note": "Use SQLite's 'PRAGMA page_count' and 'PRAGMA page_size' for database size."})
    elif db_type == DBType.POSTGRESQL:
        if table:
            return await _execute_with_pool(db_url, db_type, pool, """
                SELECT pg_size_pretty(pg_total_relation_size($1)) AS total_size,
                       pg_size_pretty(pg_relation_size($1)) AS table_size,
                       pg_size_pretty(pg_indexes_size($1)) AS index_size,
                       (SELECT count(*) FROM $1::regclass) AS row_count
            """, [table], 10, 30)
        return await _execute_with_pool(db_url, db_type, pool, """
            SELECT datname, pg_size_pretty(pg_database_size(datname)) AS size
            FROM pg_database ORDER BY pg_database_size(datname) DESC
        """, None, 500, 30)
    elif db_type == DBType.MYSQL:
        if table:
            return await _execute_with_pool(db_url, db_type, pool, f"""
                SELECT table_name, table_rows, round(((data_length + index_length) / 1024 / 1024), 2) AS size_mb
                FROM information_schema.TABLES WHERE table_schema = DATABASE() AND table_name = '{table}'
            """, None, 10, 30)
        return await _execute_with_pool(db_url, db_type, pool, """
            SELECT table_schema, SUM(table_rows) AS total_rows,
                   round(SUM(data_length + index_length) / 1024 / 1024, 2) AS total_size_mb
            FROM information_schema.TABLES WHERE table_schema = DATABASE()
            GROUP BY table_schema
        """, None, 10, 30)
    elif db_type == DBType.MSSQL:
        if table:
            return await _execute_with_pool(db_url, db_type, pool, """
                SELECT OBJECT_NAME(s.object_id) AS table_name,
                       SUM(p.rows) AS row_count,
                       CAST(ROUND(SUM(a.total_pages) * 8 / 1024.0, 2) AS DECIMAL(18,2)) AS total_size_mb
                FROM sys.indexes i JOIN sys.partitions p ON i.object_id = p.object_id AND i.index_id = p.index_id
                JOIN sys.allocation_units a ON p.partition_id = a.container_id
                JOIN sys.objects s ON i.object_id = s.object_id
                WHERE s.name = %s GROUP BY s.object_id ORDER BY s.name
            """, [table], 10, 30)
        return await _execute_with_pool(db_url, db_type, pool, "SELECT name, state_desc FROM sys.databases ORDER BY name", None, 500, 30)
    elif db_type == DBType.ORACLE:
        if table:
            return await _execute_with_pool(db_url, db_type, pool, "SELECT num_rows, last_analyzed FROM all_tables WHERE table_name = :1", [table.upper()], 10, 30)
        return json.dumps({"note": "Use DBA_DATA_FILES or user_ts_quotas for Oracle size info."})
    elif db_type == DBType.DUCKDB:
        if table:
            return await _execute_with_pool(db_url, db_type, pool, f"SELECT count(*) AS row_count FROM {_quote_ident(table)}", None, 10, 30)
        return json.dumps({"note": "Use 'SELECT * FROM duckdb_databases()' for DuckDB database info."})
    elif db_type == DBType.CLICKHOUSE:
        if table:
            return await _execute_with_pool(db_url, db_type, pool, f"SELECT count(*) AS row_count FROM {_quote_ident(table)}", None, 10, 30)
        return await _execute_with_pool(db_url, db_type, pool, "SELECT database, name, engine, total_rows, total_bytes FROM system.tables WHERE database = currentDatabase()", None, 500, 30)
    elif db_type == DBType.MONGODB:
        if table:
            return await _execute_with_pool(db_url, db_type, pool, json.dumps({"action": "estimated_count", "collection": table}), None, 10, 30)
        return json.dumps({"note": "Use db.stats() via mongodb_action=aggregate for MongoDB database stats."})
    elif db_type == DBType.REDIS:
        return await _handle_redis(pool, {"redis_command": "DBSIZE"}, 30)
    return json.dumps({"error": "Unsupported database type"})


async def _get_explain(db_url: str, db_type: DBType, pool, sql: str, params: Optional[list], timeout: int) -> str:
    if db_type == DBType.SQLITE:
        return await _execute_with_pool(db_url, db_type, pool, f"EXPLAIN QUERY PLAN {sql}", params, 500, timeout)
    elif db_type == DBType.POSTGRESQL:
        return await _execute_with_pool(db_url, db_type, pool, f"EXPLAIN (ANALYZE, BUFFERS) {sql}", params, 500, timeout)
    elif db_type == DBType.MYSQL:
        return await _execute_with_pool(db_url, db_type, pool, f"EXPLAIN {sql}", params, 500, timeout)
    elif db_type == DBType.MSSQL:
        return await _execute_with_pool(db_url, db_type, pool, f"SET SHOWPLAN_ALL ON; {sql} SET SHOWPLAN_ALL OFF", params, 500, timeout)
    elif db_type == DBType.ORACLE:
        return await _execute_with_pool(db_url, db_type, pool, f"EXPLAIN PLAN FOR {sql}", params, 500, timeout)
    elif db_type == DBType.DUCKDB:
        return await _execute_with_pool(db_url, db_type, pool, f"EXPLAIN {sql}", params, 500, timeout)
    elif db_type == DBType.CLICKHOUSE:
        return await _execute_with_pool(db_url, db_type, pool, f"EXPLAIN {sql}", params, 500, timeout)
    return json.dumps({"error": "EXPLAIN not supported for this database type"})


# ---------------------------------------------------------------------------
# Database-specific metadata
# ---------------------------------------------------------------------------

async def _get_db_metadata(db_url: str, db_type: DBType, pool, meta_action: str, timeout: int) -> str:
    sql_map = {
        DBType.POSTGRESQL: {
            "extensions": "SELECT * FROM pg_extension ORDER BY extname",
            "functions": "SELECT proname, pg_catalog.oidvectortypes(proargtypes) AS args, prorettype::regtype AS return_type FROM pg_proc WHERE pronamespace NOT IN ('pg_catalog'::regnamespace, 'information_schema'::regnamespace) ORDER BY proname",
            "sequences": "SELECT sequence_name FROM information_schema.sequences WHERE sequence_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY sequence_name",
            "schemata": "SELECT schema_name FROM information_schema.schemata WHERE schema_name NOT IN ('pg_catalog', 'information_schema') ORDER BY schema_name",
            "locks": "SELECT locktype, relation::regclass AS relation, mode, granted FROM pg_locks WHERE locktype != 'virtualxid' AND locktype != 'virtualid' ORDER BY locktype",
        },
        DBType.MYSQL: {
            "engines": "SHOW ENGINES",
            "variables": "SHOW VARIABLES",
            "status": "SHOW GLOBAL STATUS",
            "processlist": "SHOW FULL PROCESSLIST",
            "charset": "SHOW CHARACTER SET",
            "collation": "SHOW COLLATION",
        },
        DBType.MSSQL: {
            "schemas": "SELECT name FROM sys.schemas ORDER BY name",
            "procedures": "SELECT name FROM sys.procedures ORDER BY name",
        },
        DBType.ORACLE: {
            "schemas": "SELECT username FROM all_users ORDER BY username",
            "sequences": "SELECT sequence_name FROM all_sequences WHERE sequence_owner = USER ORDER BY sequence_name",
        },
        DBType.SQLITE: {
            "pragmas": "SELECT * FROM pragma_compile_options",
            "user_info": "SELECT * FROM pragma_database_list",
        },
        DBType.DUCKDB: {
            "settings": "SELECT * FROM duckdb_settings()",
            "extensions": "SELECT * FROM duckdb_extensions()",
        },
        DBType.CLICKHOUSE: {
            "databases": "SHOW DATABASES",
            "processes": "SHOW PROCESSLIST",
            "functions": "SHOW FUNCTIONS",
        },
    }
    db_meta = sql_map.get(db_type, {})
    sql = db_meta.get(meta_action)
    if sql:
        return await _execute_with_pool(db_url, db_type, pool, sql, None, 500, timeout)
    return json.dumps({"error": f"Metadata action '{meta_action}' not available for {db_type.value}"})


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

async def _export_data(db_url: str, db_type: DBType, pool, sql: str, params: Optional[list],
                       export_format: str, limit: int, timeout: int) -> str:
    result = await _execute_with_pool(db_url, db_type, pool, sql, params, limit, timeout)
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return result

    if "error" in data:
        return result

    columns = data.get("columns", [])
    rows = data.get("rows", [])

    if export_format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        if columns:
            writer.writerow(columns)
        for row in rows:
            writer.writerow([row.get(c, "") for c in columns])
        csv_content = output.getvalue()
        return json.dumps({"format": "csv", "content": csv_content, "count": len(rows)})

    elif export_format == "json":
        return json.dumps({"format": "json", "data": rows, "count": len(rows)}, indent=2, default=str)

    elif export_format == "markdown":
        if not columns:
            return json.dumps({"format": "markdown", "content": "(no results)", "count": 0})
        lines = []
        header = "| " + " | ".join(columns) + " |"
        sep = "| " + " | ".join(["---"] * len(columns)) + " |"
        lines.append(header)
        lines.append(sep)
        for row in rows:
            vals = [str(row.get(c, "")) for c in columns]
            lines.append("| " + " | ".join(vals) + " |")
        content = "\n".join(lines)
        return json.dumps({"format": "markdown", "content": content, "count": len(rows)})

    return result


# ---------------------------------------------------------------------------
# Quote helper
# ---------------------------------------------------------------------------

def _quote_ident(name: str) -> str:
    return f'"{name}"'


def _db_name(db_url: str) -> str:
    try:
        _, params = _parse_db_url(db_url)
        return params.get("database", "test")
    except Exception:
        return "test"


# ---------------------------------------------------------------------------
# Create database
# ---------------------------------------------------------------------------

async def _create_database(db_url: str, db_type: DBType, new_db: str, timeout: int) -> str:
    if db_type == DBType.SQLITE:
        return json.dumps({"status": "ok", "database": new_db, "message": "SQLite: database will be created on first connection. Use sqlite:///path in database_url."})

    if db_type == DBType.DUCKDB:
        return json.dumps({"status": "ok", "database": new_db, "message": "DuckDB: database will be created on first connection. Use duckdb:///path in database_url."})

    if db_type == DBType.MONGODB:
        return json.dumps({"status": "ok", "database": new_db, "message": "MongoDB: databases are auto-created on first use. Connect with mongodb://host:port/" + new_db})

    if db_type == DBType.REDIS:
        return json.dumps({"note": "Redis databases are index-based (0-15 by default). Use redis://host:port/index to select a DB index."})

    if db_type == DBType.ORACLE:
        return json.dumps({"note": "Oracle uses service names. Set the service name in the URL path, e.g. oracle://user:pass@host:1521/service_name"})

    if db_type == DBType.POSTGRESQL:
        return await _create_pg_database(db_url, new_db, timeout)

    if db_type == DBType.MYSQL:
        return await _create_mysql_database(db_url, new_db, timeout)

    if db_type == DBType.MSSQL:
        return await _create_mssql_database(db_url, new_db, timeout)

    if db_type == DBType.CLICKHOUSE:
        return await _create_clickhouse_database(db_url, new_db, timeout)

    return json.dumps({"error": f"create_database not supported for {db_type.value}"})


async def _create_pg_database(db_url: str, new_db: str, timeout: int) -> str:
    import asyncpg
    _, params = _parse_db_url(db_url)
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(host=params["host"], port=params["port"],
                            user=params["user"], password=params["password"],
                            database="postgres"),
            timeout=timeout,
        )
        try:
            await conn.execute(f'CREATE DATABASE {_quote_ident(new_db)}')
            return json.dumps({"status": "ok", "database": new_db})
        except Exception as e:
            return json.dumps({"error": f"Failed to create database: {e}"})
        finally:
            await conn.close()
    except asyncio.TimeoutError:
        return json.dumps({"error": f"Connection to PostgreSQL timed out after {timeout}s"})
    except Exception as e:
        return json.dumps({"error": f"Failed to connect to PostgreSQL: {e}"})


async def _create_mysql_database(db_url: str, new_db: str, timeout: int) -> str:
    import aiomysql
    _, params = _parse_db_url(db_url)
    try:
        conn = await asyncio.wait_for(
            aiomysql.connect(host=params["host"], port=params["port"],
                             user=params["user"], password=params["password"],
                             db="mysql"),
            timeout=timeout,
        )
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(f'CREATE DATABASE {_quote_ident(new_db)}')
            await conn.commit()
            return json.dumps({"status": "ok", "database": new_db})
        except Exception as e:
            return json.dumps({"error": f"Failed to create database: {e}"})
        finally:
            conn.close()
    except asyncio.TimeoutError:
        return json.dumps({"error": f"Connection to MySQL timed out after {timeout}s"})
    except Exception as e:
        return json.dumps({"error": f"Failed to connect to MySQL: {e}"})


async def _create_mssql_database(db_url: str, new_db: str, timeout: int) -> str:
    import pymssql
    _, params = _parse_db_url(db_url)

    def _create() -> str:
        conn = pymssql.connect(server=params["host"], port=params["port"],
                               user=params["user"], password=params["password"],
                               database="master")
        try:
            with conn.cursor() as cursor:
                cursor.execute(f'CREATE DATABASE {_quote_ident(new_db)}')
            conn.commit()
            return json.dumps({"status": "ok", "database": new_db})
        except Exception as e:
            return json.dumps({"error": f"Failed to create database: {e}"})
        finally:
            conn.close()

    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _create), timeout=timeout)
    except asyncio.TimeoutError:
        return json.dumps({"error": f"Connection to SQL Server timed out after {timeout}s"})


async def _create_clickhouse_database(db_url: str, new_db: str, timeout: int) -> str:
    from clickhouse_driver import Client as CHClient
    _, params = _parse_db_url(db_url)

    def _create() -> str:
        client = CHClient(host=params["host"], port=params["port"],
                          user=params["user"], password=params["password"],
                          database="default")
        try:
            client.execute(f'CREATE DATABASE IF NOT EXISTS {_quote_ident(new_db)}')
            return json.dumps({"status": "ok", "database": new_db})
        except Exception as e:
            return json.dumps({"error": f"Failed to create database: {e}"})
        finally:
            client.disconnect()

    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _create), timeout=timeout)
    except asyncio.TimeoutError:
        return json.dumps({"error": f"Connection to ClickHouse timed out after {timeout}s"})


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def _execute_with_pool(db_url: str, db_type: DBType, pool, sql: str, params: Optional[list], limit: int, timeout: int) -> str:
    dispatch = {
        DBType.SQLITE: _execute_sqlite,
        DBType.POSTGRESQL: _execute_postgres,
        DBType.MYSQL: _execute_mysql,
        DBType.MSSQL: _execute_mssql,
        DBType.ORACLE: _execute_oracle,
        DBType.DUCKDB: _execute_duckdb,
        DBType.CLICKHOUSE: _execute_clickhouse,
    }
    handler = dispatch.get(db_type)
    if handler:
        return await handler(pool, sql, params, limit, timeout)
    return json.dumps({"error": f"Unsupported database type: {db_type}"})


async def _database_execute(**kwargs: Any) -> str:
    try:
        return await _database_execute_impl(**kwargs)
    except Exception as e:
        return json.dumps({"error": f"Database error: {e}"})


async def _database_execute_impl(**kwargs: Any) -> str:
    action = kwargs.get("action", "query")
    sql = kwargs.get("sql", "")
    database_url = kwargs.get("database_url", "")
    limit = kwargs.get("limit", 100)
    read_only = kwargs.get("read_only", False)
    timeout = kwargs.get("timeout", 30)
    params = kwargs.get("params", None)
    table = kwargs.get("table", "")

    valid_actions = {
        "query", "execute", "tables", "views", "indexes", "describe", "explain",
        "size", "export", "metadata", "ping", "close_pool", "close_all",
        "list_databases", "create_database", "mongodb", "redis",
    }
    if action not in valid_actions:
        return json.dumps({"error": f"Unknown action: {action}. Valid: {sorted(valid_actions)}"})

    if action in ("query", "execute", "explain", "export") and not sql:
        return json.dumps({"error": "sql is required for this action"})

    if action in ("describe", "indexes", "size") and not table and action != "size":
        pass

    try:
        db_type, _ = _parse_db_url(database_url)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    if action == "ping":
        return json.dumps({"status": "ok", "database_url": database_url or ":memory:", "database_type": db_type.value})

    if action == "close_pool":
        return await _close_pool(database_url)

    if action == "close_all":
        return await _close_all_pools()

    if action == "list_databases":
        if db_type == DBType.SQLITE:
            return json.dumps({"databases": [database_url or ":memory:"]})
        elif db_type == DBType.POSTGRESQL:
            return await _execute_with_pool(database_url, db_type, None, "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname", None, 500, timeout)
        elif db_type == DBType.MYSQL:
            return await _execute_with_pool(database_url, db_type, None, "SHOW DATABASES", None, 500, timeout)
        elif db_type == DBType.MSSQL:
            return await _execute_with_pool(database_url, db_type, None, "SELECT name FROM sys.databases ORDER BY name", None, 500, timeout)
        elif db_type == DBType.ORACLE:
            return json.dumps({"databases": [database_url], "note": "Oracle uses service names. Set the service in the URL path."})
        elif db_type == DBType.DUCKDB:
            return json.dumps({"databases": [database_url or ":memory:"]})
        elif db_type == DBType.CLICKHOUSE:
            return await _execute_with_pool(database_url, db_type, None, "SHOW DATABASES", None, 500, timeout)
        elif db_type == DBType.MONGODB:
            return json.dumps({"note": "Use mongodb_action=collections to list collections."})
        elif db_type == DBType.REDIS:
            return json.dumps({"databases": [database_url], "note": "Redis db index is set in the URL path."})
        return json.dumps({"error": "Unsupported database type"})

    try:
        pool = await _get_pool(database_url)
    except ImportError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to connect: {e}"})

    if action == "tables":
        return await _list_tables(database_url, db_type, pool)

    if action == "views":
        return await _list_views(database_url, db_type, pool)

    if action == "indexes":
        return await _list_indexes(database_url, db_type, pool, table)

    if action == "describe":
        if not table:
            return json.dumps({"error": "table is required for describe action"})
        return await _describe_table(database_url, db_type, pool, table)

    if action == "size":
        return await _get_size(database_url, db_type, pool, table)

    if action == "explain":
        return await _get_explain(database_url, db_type, pool, sql, params, timeout)

    if action == "metadata":
        meta_action = kwargs.get("meta_action", "")
        if not meta_action:
            return json.dumps({"error": "meta_action is required. Common values depend on database type."})
        return await _get_db_metadata(database_url, db_type, pool, meta_action, timeout)

    if action == "export":
        export_format = kwargs.get("export_format", "json")
        if export_format not in ("json", "csv", "markdown"):
            return json.dumps({"error": "export_format must be 'json', 'csv', or 'markdown'"})
        return await _export_data(database_url, db_type, pool, sql, params, export_format, limit, timeout)

    if action == "create_database":
        new_db = kwargs.get("new_database", "")
        if not new_db:
            return json.dumps({"error": "new_database parameter is required for create_database action"})
        return await _create_database(database_url, db_type, new_db, timeout)

    if action == "mongodb":
        return await _handle_mongodb(pool, kwargs, timeout)

    if action == "redis":
        return await _handle_redis(pool, kwargs, timeout)

    if read_only and not _is_read_only_query(sql):
        return json.dumps({"error": "Read-only mode: only SELECT, PRAGMA, EXPLAIN, SHOW, DESCRIBE, WITH allowed."})

    if _is_dangerous_ddl(sql):
        return json.dumps({"error": "Dangerous DDL blocked (DROP DATABASE/SCHEMA/USER/ROLE, GRANT/REVOKE, ALTER SYSTEM, etc.)"})

    return await _execute_with_pool(database_url, db_type, pool, sql, params, limit, timeout)


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------

database_tool = build_tool(
    name="database",
    description=(
        "Execute SQL queries, DDL/DML, and NoSQL operations against connected "
        "relational, document, or key-value databases. "
        "Use this for inspecting schemas (tables/views/indexes/describe), running "
        "SELECTs (`query`), INSERT/UPDATE/DELETE/DDL (`execute`), explaining plans, "
        "exporting results, or driving MongoDB/Redis via dedicated sub-actions. "
        "Do NOT use this for application-level ORM calls, file-based data analysis "
        "(use spreadsheet/pandas), or migrating schemas across environments. "
        "Tips: pass values via `params` instead of interpolating SQL; cap `limit` to "
        "avoid huge payloads; set `read_only=true` to enforce SELECT-only safety. "
        "Pitfalls: dangerous DDL (DROP DATABASE/SCHEMA/USER, GRANT/REVOKE, ALTER "
        "SYSTEM) is blocked; queries time out after `timeout` seconds (default 30)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "query", "execute", "tables", "views", "indexes", "describe",
                    "explain", "size", "export", "metadata", "ping",
                    "close_pool", "close_all", "list_databases", "create_database",
                    "mongodb", "redis",
                ],
                "description": (
                    "Database operation to perform. query: SELECT/WITH. execute: INSERT/UPDATE/DELETE/DDL. "
                    "tables: list tables. views: list views. indexes: list indexes. "
                    "describe: show table schema. explain: query plan. "
                    "size: table/database size. export: export as json/csv/markdown. "
                    "metadata: db-specific info (engines, variables, processlist, etc). "
                    "ping: test connection. close_pool/close_all: manage connection pools. "
                    "list_databases: list databases. "
                    "create_database: create a new database (requires new_database param). "
                    "mongodb: MongoDB operations. redis: Redis operations."
                ),
                "default": "query",
            },
            "sql": {
                "type": "string",
                "description": "SQL statement text; required for query, execute, explain, and export actions.",
            },
            "params": {
                "type": "array",
                "items": {},
                "description": "Positional parameters bound to placeholders (?) in the SQL; preferred over string interpolation for safety.",
            },
            "database_url": {
                "type": "string",
                "description": (
                    "Connection URL for the target database.\n"
                    "  sqlite:///path | sqlite:///:memory:\n"
                    "  postgresql://user:pass@host:port/db\n"
                    "  mysql://user:pass@host:port/db\n"
                    "  mssql://user:pass@host:port/db\n"
                    "  oracle://user:pass@host:1521/service\n"
                    "  duckdb:///path | duckdb:///:memory:\n"
                    "  clickhouse://user:pass@host:9000/db\n"
                    "  mongodb://user:pass@host:27017/db\n"
                    "  redis://user:pass@host:6379/0\n"
                    "Defaults to in-memory SQLite when omitted."
                ),
            },
            "table": {
                "type": "string",
                "description": "Target table name used by describe, indexes, and size actions.",
            },
            "new_database": {
                "type": "string",
                "description": "Name of the database to create; required when action=create_database.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of rows to return per result set; caps memory usage.",
                "default": 100,
            },
            "read_only": {
                "type": "boolean",
                "description": "If true, restrict execution to SELECT/PRAGMA/EXPLAIN/SHOW/DESCRIBE/WITH statements only.",
                "default": False,
            },
            "timeout": {
                "type": "integer",
                "description": "Per-query timeout in seconds before the statement is cancelled.",
                "default": 30,
            },
            "export_format": {
                "type": "string",
                "enum": ["json", "csv", "markdown"],
                "description": "Output format used when action=export.",
            },
            "meta_action": {
                "type": "string",
                "description": (
                    "Database-specific metadata sub-action when action=metadata.\n"
                    "PG: extensions, functions, sequences, schemata, locks\n"
                    "MySQL: engines, variables, status, processlist, charset, collation\n"
                    "MSSQL: schemas, procedures\n"
                    "Oracle: schemas, sequences\n"
                    "ClickHouse: databases, processes, functions\n"
                    "SQLite: pragmas, user_info\n"
                    "DuckDB: settings, extensions"
                ),
            },
            # MongoDB
            "mongodb_action": {
                "type": "string",
                "enum": [
                    "find", "find_one", "find_one_and_update", "find_one_and_delete",
                    "find_one_and_replace", "insert", "update", "delete",
                    "aggregate", "collections", "create_collection", "drop_collection",
                    "rename_collection", "count", "estimated_count", "distinct",
                    "indexes", "create_index", "drop_index", "bulk_write", "watch",
                ],
                "description": "MongoDB operation to run; only consulted when action=mongodb.",
            },
            "mongodb_db": {
                "type": "string",
                "description": "MongoDB database name; falls back to the database segment of the connection URL.",
            },
            "mongodb_collection": {
                "type": "string",
                "description": "MongoDB collection name to operate on.",
            },
            "mongodb_filter": {
                "type": "string",
                "description": "MongoDB query filter expressed as a JSON string (e.g. '{\"status\": \"active\"}').",
            },
            "mongodb_document": {
                "type": "string",
                "description": "Document or array of documents to insert, encoded as a JSON string.",
            },
            "mongodb_update": {
                "type": "string",
                "description": "MongoDB update operators document (e.g. '{\"$set\": {...}}') as a JSON string.",
            },
            "mongodb_replacement": {
                "type": "string",
                "description": "Replacement document for find_one_and_replace, as a JSON string.",
            },
            "mongodb_multi": {
                "type": "boolean",
                "description": "If true, update or delete multiple matching documents (delete defaults to true, update to false).",
                "default": False,
            },
            "mongodb_pipeline": {
                "type": "string",
                "description": "Aggregation pipeline stages as a JSON array string.",
            },
            "mongodb_field": {
                "type": "string",
                "description": "Field name used by the distinct operation.",
            },
            "mongodb_sort": {
                "type": "string",
                "description": "Sort specification as a JSON list, e.g. '[[\"field\", 1]]'.",
            },
            "mongodb_projection": {
                "type": "string",
                "description": "Projection document as a JSON object, e.g. '{\"field\": 1}'.",
            },
            "mongodb_skip": {
                "type": "integer",
                "description": "Number of documents to skip before returning results.",
                "default": 0,
            },
            "mongodb_return_document": {
                "type": "string",
                "enum": ["before", "after"],
                "description": "Whether to return the document before or after the update/replace is applied.",
                "default": "after",
            },
            "mongodb_new_name": {
                "type": "string",
                "description": "New collection name for rename_collection.",
            },
            "mongodb_keys": {
                "type": "string",
                "description": "Index key specification as a JSON list, e.g. '[[\"field\", 1]]'.",
            },
            "mongodb_index_name": {
                "type": "string",
                "description": "Name of the index to create or drop.",
            },
            "mongodb_unique": {
                "type": "boolean",
                "description": "If true, create a unique index.",
                "default": False,
            },
            "mongodb_operations": {
                "type": "string",
                "description": "Bulk write operation descriptors as a JSON array string.",
            },
            # Redis
            "redis_command": {
                "type": "string",
                "description": "Redis command name (e.g. GET, SET, KEYS, DEL, LPUSH, SADD, HSET, SCAN, INFO).",
            },
            "redis_args": {
                "type": "array",
                "items": {},
                "description": "Positional arguments for the Redis command, in the order expected by the server.",
            },
        },
    },
    execute=_database_execute,
    intents=["coding", "data"],
    is_concurrency_safe=lambda _: True,
    category="data",
    semantic_type="exec",
    is_destructive=lambda args: (
        args.get("action", "query") == "execute"
        and not args.get("read_only", False)
    ) or args.get("action") in ("mongodb", "redis", "create_database"),
)
