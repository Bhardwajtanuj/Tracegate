from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tracegate.harness import RunContext
from tracegate.models import LLMResponse
from tracegate.tools import ToolRegistry

Policy = Callable[[list[dict[str, Any]]], dict[str, Any]]

REVENUE_ROWS = [
    ("north", "Q1", 100000),
    ("north", "Q2", 120000),
    ("north", "Q3", 150000),
    ("south", "Q1", 80000),
    ("south", "Q2", 90000),
    ("south", "Q3", 110000),
    ("east", "Q1", 70000),
    ("east", "Q2", 85000),
    ("east", "Q3", 95000),
    ("west", "Q1", 50000),
    ("west", "Q2", 52000),
    ("west", "Q3", 57000),
]

DOCUMENTS = [
    {
        "id": "rollback-guide",
        "title": "Deployment rollback procedure",
        "text": "Revert to the previous release tag, redeploy and verify the health checks.",
    },
    {
        "id": "incident-runbook",
        "title": "Incident response runbook",
        "text": "Page the on-call engineer, open an incident channel and record a timeline.",
    },
    {
        "id": "onboarding-checklist",
        "title": "New engineer onboarding",
        "text": "Request repository access, set up the local toolchain and read the architecture.",
    },
    {
        "id": "data-retention",
        "title": "Data retention policy",
        "text": "Operational logs are retained for ninety days and analytics tables for two years.",
    },
]

EMAIL_PATTERN = r"[\w.+-]+@[\w-]+\.[\w.-]+"
REVENUE_WORDS = ("revenue", "sales", "database", "query")
DOCUMENT_WORDS = ("runbook", "documentation", "docs", "how do i", "procedure")
EMAIL_WORDS = ("email", "alert", "notify")
STOPWORDS = {"the", "for", "and", "with", "from", "that", "this", "are", "how", "can"}


class DatabaseQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_string: str = Field(min_length=10)
    row_limit: int = Field(default=100, ge=1, le=500)

    @field_validator("query_string")
    @classmethod
    def read_only(cls, value: str) -> str:
        statement = value.strip().rstrip(";").strip()
        if ";" in statement:
            raise ValueError("multiple statements are not allowed")
        if not statement.lower().startswith("select"):
            raise ValueError("only SELECT statements are allowed")
        return statement


class SendEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)


class SearchDocuments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=500)
    top_k: int = Field(default=3, ge=1, le=10)


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if token not in STOPWORDS}


def build_database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.execute("CREATE TABLE revenue (region TEXT, quarter TEXT, amount INTEGER)")
    connection.executemany("INSERT INTO revenue VALUES (?, ?, ?)", REVENUE_ROWS)
    connection.commit()
    connection.execute("PRAGMA query_only = ON")
    return connection


def build_registry(connection: sqlite3.Connection, outbox: list[dict[str, Any]]) -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register(
        "query_database",
        DatabaseQuery,
        "Run a read-only SQL SELECT against the revenue table (region, quarter, amount).",
    )
    def query_database(args: DatabaseQuery) -> dict[str, Any]:
        cursor = connection.execute(args.query_string)
        columns = [column[0] for column in cursor.description or []]
        rows = [list(row) for row in cursor.fetchmany(args.row_limit)]
        return {"columns": columns, "rows": rows}

    @registry.register(
        "send_email",
        SendEmail,
        "Queue an email message for delivery to a single recipient.",
    )
    def send_email(args: SendEmail) -> dict[str, Any]:
        message_id = f"msg-{len(outbox) + 1}"
        outbox.append({"id": message_id, **args.model_dump()})
        return {"status": "queued", "message_id": message_id, "recipient": args.recipient}

    @registry.register(
        "search_documents",
        SearchDocuments,
        "Search the internal documentation corpus and return the best matching documents.",
    )
    def search_documents(args: SearchDocuments) -> list[dict[str, Any]]:
        wanted = _tokens(args.query)
        scored = []
        for document in DOCUMENTS:
            score = len(wanted & _tokens(f"{document['title']} {document['text']}"))
            if score:
                scored.append((score, document))
        scored.sort(key=lambda item: (-item[0], item[1]["id"]))
        return [
            {"id": document["id"], "title": document["title"], "score": score}
            for score, document in scored[: args.top_k]
        ]

    return registry


def _final(answer: str) -> dict[str, Any]:
    return {"action": "final", "answer": answer}


def _call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"action": "tool", "tool": tool, "arguments": arguments}


def _plan(lowered: str) -> list[str]:
    plan = []
    if any(word in lowered for word in REVENUE_WORDS):
        plan.append("query_database")
    if any(word in lowered for word in DOCUMENT_WORDS):
        plan.append("search_documents")
    if any(word in lowered for word in EMAIL_WORDS):
        plan.append("send_email")
    return plan


def _summarize(message: dict[str, Any]) -> str:
    payload = json.loads(message["content"])
    if payload["status"] == "ok":
        return f"{message['name']}: {json.dumps(payload['output'], sort_keys=True)}"
    return f"{message['name']} failed with {payload['status']}"


def _arguments(tool: str, request: str, observations: list[dict[str, Any]]) -> dict[str, Any]:
    if tool == "query_database":
        quarter = re.search(r"\bq([1-4])\b", request.lower())
        if quarter:
            query = (
                f"SELECT SUM(amount) AS total FROM revenue WHERE quarter = 'Q{quarter.group(1)}'"
            )
        else:
            query = (
                "SELECT quarter, SUM(amount) AS total FROM revenue "
                "GROUP BY quarter ORDER BY quarter"
            )
        return {"query_string": query, "row_limit": 100}
    if tool == "search_documents":
        return {"query": request, "top_k": 3}
    address = re.search(EMAIL_PATTERN, request)
    body = " ".join(_summarize(message) for message in observations) or request
    return {
        "recipient": address.group(0) if address else "admin@example.com",
        "subject": "Automated alert",
        "body": body,
    }


def keyword_policy(messages: list[dict[str, Any]]) -> dict[str, Any]:
    request = messages[1]["content"]
    plan = _plan(request.lower())
    observations = [message for message in messages if message["role"] == "tool"]
    if not plan:
        return _final("I can only help with revenue queries, document search and alert emails.")
    completed = {message["name"] for message in observations}
    pending = [tool for tool in plan if tool not in completed]
    if not pending:
        return _final(" ".join(_summarize(message) for message in observations))
    return _call(pending[0], _arguments(pending[0], request, observations))


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class ReferenceAgent:
    def __init__(self, policy: Policy = keyword_policy, model: str = "mock-llm"):
        self.policy = policy
        self.model = model
        self.outbox: list[dict[str, Any]] = []
        self.database = build_database()
        self.tools = build_registry(self.database, self.outbox)

    def _complete(self, messages: list[dict[str, Any]]) -> LLMResponse:
        action = self.policy(messages)
        text = json.dumps(action)
        return LLMResponse(
            text=text,
            input_tokens=estimate_tokens(json.dumps(messages)),
            output_tokens=estimate_tokens(text),
        )

    def run(self, ctx: RunContext, user_input: str) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "Plan tool calls and answer the user."},
            {"role": "user", "content": user_input},
        ]
        while True:
            response = ctx.llm(self.model, messages, lambda: self._complete(messages))
            action = json.loads(response.text)
            messages.append({"role": "assistant", "content": response.text})
            if action["action"] == "final":
                return action["answer"]
            result = ctx.tool(action["tool"], action["arguments"])
            messages.append(
                {"role": "tool", "name": action["tool"], "content": result.model_dump_json()}
            )


def create_agent() -> ReferenceAgent:
    return ReferenceAgent()
