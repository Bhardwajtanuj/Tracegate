from __future__ import annotations

import sqlite3

import pytest
from pydantic import BaseModel, Field

from tracegate import ToolRegistry
from tracegate.reference import DatabaseQuery, build_database, build_registry


class Add(BaseModel):
    a: int
    b: int = Field(default=1, ge=0)


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register("add", Add, "Add two numbers")
    def add(args: Add) -> dict:
        return {"sum": args.a + args.b}

    @registry.register("explode", Add)
    def explode(args: Add) -> None:
        raise RuntimeError("boom")

    return registry


def test_successful_execution():
    result = make_registry().execute("add", {"a": 2, "b": 3})
    assert result.ok
    assert result.output == {"sum": 5}


def test_invalid_arguments_return_structured_errors():
    result = make_registry().execute("add", {"a": "x", "b": -1})
    assert result.status == "argument_error"
    assert result.retryable
    locations = {tuple(error["loc"]) for error in result.errors}
    assert ("a",) in locations
    assert ("b",) in locations
    assert all("url" not in error and "input" not in error for error in result.errors)


def test_non_mapping_arguments_are_rejected_cleanly():
    assert make_registry().execute("add", "not a dict").status == "argument_error"
    assert make_registry().execute("add", None).status == "argument_error"


def test_unknown_tool():
    result = make_registry().execute("missing", {})
    assert result.status == "unknown_tool"
    assert result.retryable


def test_handler_exceptions_do_not_escape():
    result = make_registry().execute("explode", {"a": 1})
    assert result.status == "tool_error"
    assert result.errors[0]["type"] == "RuntimeError"
    assert not result.retryable


def test_duplicate_registration_is_rejected():
    registry = make_registry()
    with pytest.raises(ValueError):
        registry.register("add", Add)(lambda args: None)


def test_schemas_expose_json_schema():
    schemas = make_registry().schemas()
    assert {schema["name"] for schema in schemas} == {"add", "explode"}
    add = next(schema for schema in schemas if schema["name"] == "add")
    assert add["parameters"]["properties"]["a"]["type"] == "integer"
    assert "add" in make_registry()


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM revenue WHERE 1=1",
        "SELECT 1; DROP TABLE revenue",
        "UPDATE revenue SET amount = 0",
    ],
)
def test_database_query_rejects_writes(query):
    registry = build_registry(build_database(), [])
    assert registry.execute("query_database", {"query_string": query}).status == "argument_error"


def test_database_query_runs_and_respects_row_limit():
    registry = build_registry(build_database(), [])
    result = registry.execute(
        "query_database",
        {"query_string": "SELECT region, amount FROM revenue ORDER BY amount", "row_limit": 2},
    )
    assert result.ok
    assert len(result.output["rows"]) == 2
    assert result.output["columns"] == ["region", "amount"]


def test_database_is_read_only_at_the_connection_level():
    connection = build_database()
    with pytest.raises(sqlite3.OperationalError):
        connection.execute("DELETE FROM revenue")


def test_database_query_model_normalizes_statement():
    assert DatabaseQuery(query_string="SELECT 1 FROM revenue;").query_string == (
        "SELECT 1 FROM revenue"
    )


def test_email_and_search_tools():
    outbox: list = []
    registry = build_registry(build_database(), outbox)
    bad = registry.execute("send_email", {"recipient": "nope", "subject": "s", "body": "b"})
    assert bad.status == "argument_error"
    good = registry.execute("send_email", {"recipient": "a@b.co", "subject": "s", "body": "b"})
    assert good.output["message_id"] == "msg-1"
    assert len(outbox) == 1
    found = registry.execute("search_documents", {"query": "rollback procedure"})
    assert found.output[0]["id"] == "rollback-guide"
    assert registry.execute("search_documents", {"query": "zzzzqq"}).output == []
