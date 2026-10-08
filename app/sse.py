# Transport framing: turns pipeline events into Server-Sent Events (ADR-004, REQ-032).
#
# Wire format, one frame per event:
#   event: <name>\n
#   data: <single-line JSON>\n
#   \n
# json.dumps never emits raw newlines, so each frame's data is exactly one line and a
# frame can't be split or forged by content (answer text with "\n\nevent: done" inside
# stays inside the JSON string). Event names come only from app/chat.py.

import json

# Sent with every SSE response: no caching, and no buffering by any reverse proxy,
# so each frame reaches the client as soon as it is produced (REQ-031).
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def frame(event: dict) -> str:
    data = json.dumps(event["data"], ensure_ascii=False, separators=(",", ":"))
    return f"event: {event['event']}\ndata: {data}\n\n"
