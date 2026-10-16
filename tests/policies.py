from __future__ import annotations

from typing import Any

from tracegate.reference import keyword_policy

QUERY = {"query_string": "SELECT SUM(amount) AS total FROM revenue", "row_limit": 10}
DOCS = {"query": "rollback procedure", "top_k": 2}


def tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"action": "tool", "tool": name, "arguments": arguments}


def looping_policy(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return tool("query_database", QUERY)


def alternating_policy(messages: list[dict[str, Any]]) -> dict[str, Any]:
    count = sum(1 for message in messages if message["role"] == "tool")
    if count % 2 == 0:
        return tool("query_database", QUERY)
    return tool("search_documents", DOCS)


def drifting_policy(messages: list[dict[str, Any]]) -> dict[str, Any]:
    count = sum(1 for message in messages if message["role"] == "tool")
    return tool(
        "query_database",
        {"query_string": f"SELECT {count} AS step_value FROM revenue", "row_limit": 5},
    )


def fumbling_policy(messages: list[dict[str, Any]]) -> dict[str, Any]:
    count = sum(1 for message in messages if message["role"] == "tool")
    if count == 0:
        return tool("query_database", {"query_string": "SELECT 1", "row_limit": 5})
    if count == 1:
        return tool(
            "query_database",
            {"query_string": "SELECT SUM(amount) AS total FROM revenue WHERE quarter = 'Q3'"},
        )
    return {"action": "final", "answer": "total 412000"}


def unknown_tool_policy(messages: list[dict[str, Any]]) -> dict[str, Any]:
    count = sum(1 for message in messages if message["role"] == "tool")
    if count == 0:
        return tool("delete_everything", {})
    return {"action": "final", "answer": "gave up"}


def crashing_policy(messages: list[dict[str, Any]]) -> dict[str, Any]:
    raise RuntimeError("policy exploded")


def wasteful_policy(messages: list[dict[str, Any]]) -> dict[str, Any]:
    count = sum(1 for message in messages if message["role"] == "tool")
    if count < 3:
        return tool(
            "query_database",
            {"query_string": f"SELECT {count + 1} AS n FROM revenue", "row_limit": 1},
        )
    return {"action": "final", "answer": "done"}


__all__ = [
    "alternating_policy",
    "crashing_policy",
    "drifting_policy",
    "fumbling_policy",
    "keyword_policy",
    "looping_policy",
    "unknown_tool_policy",
    "wasteful_policy",
]
