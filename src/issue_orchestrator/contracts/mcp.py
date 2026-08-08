"""Typed payload fragments shared by the public MCP tool boundary."""

from typing import Literal, NotRequired, TypedDict


class McpErrorPayload(TypedDict):
    """Structured error returned by MCP tools."""

    message: str
    type: str


class McpUiHintPayload(TypedDict):
    """Optional client navigation hint attached to a failed MCP start."""

    kind: Literal["doctor"]
    url: NotRequired[str]
