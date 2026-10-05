"""CallToolResult → HTTP response conversion."""

from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent
from starlette.responses import JSONResponse, Response


def serialize_tool_result(result: CallToolResult, definer_tier: int = 0, canonical: str = "") -> Response:
    """Convert a CallToolResult to an HTTP response.

    - Single text block → try JSON parse, else return as string
    - Multiple content blocks → array of typed objects
    - Image blocks → base64 data with MIME type
    - Tool errors (isError=true) → HTTP 422
    """
    status_code = 422 if result.isError else 200
    body: dict[str, Any] = {}

    content = result.content

    if len(content) == 0:
        body["result"] = None
    elif len(content) == 1:
        block = content[0]
        body["result"] = _serialize_block(block)
    else:
        body["result"] = [_serialize_block(b) for b in content]

    if result.isError:
        body["error"] = True

    if definer_tier > 0:
        body["definer_tier"] = definer_tier
    if canonical:
        body["canonical"] = canonical

    headers = {}
    if definer_tier > 0:
        headers["X-Gimme-Definer-Tier"] = str(definer_tier)
    if canonical:
        headers["X-Gimme-Definer-Canonical"] = canonical

    return JSONResponse(body, status_code=status_code, headers=headers)


def _serialize_block(block) -> Any:
    """Serialize a single content block."""
    if isinstance(block, TextContent):
        text = block.text
        # Try parsing as JSON
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text
    elif isinstance(block, ImageContent):
        return {
            "type": "image",
            "mimeType": block.mimeType,
            "data": block.data,
        }
    else:
        # EmbeddedResource or other types — return as dict
        return block.model_dump() if hasattr(block, "model_dump") else str(block)
