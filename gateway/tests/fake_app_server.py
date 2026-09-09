from __future__ import annotations

import json
import sys


def send(payload):
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


for raw_line in sys.stdin:
    message = json.loads(raw_line)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialized":
        continue
    if method == "initialize":
        send(
            {
                "id": request_id,
                "result": {
                    "userAgent": "fake",
                    "codexHome": "/fake",
                    "platformFamily": "unix",
                    "platformOs": "linux",
                },
            }
        )
    elif method == "thread/list":
        send(
            {
                "id": request_id,
                "result": {
                    "data": [
                        {
                            "id": "raw-thread-from-stdio",
                            "name": "Stdio thread",
                            "preview": "Handshake works",
                            "status": {"type": "notLoaded"},
                        }
                    ],
                    "nextCursor": None,
                },
            }
        )
    elif method == "thread/read" and (message.get("params") or {}).get(
        "threadId"
    ) == "raw-thread-large-frame":
        send(
            {
                "id": request_id,
                "result": {
                    "thread": {
                        "id": "raw-thread-large-frame",
                        "name": "Large frame",
                        "preview": "Large frame",
                        "status": {"type": "notLoaded"},
                        "turns": [
                            {
                                "id": "raw-turn-large-frame",
                                "status": "completed",
                                "startedAt": 1784419200,
                                "completedAt": 1784419201,
                                "items": [
                                    {
                                        "id": "raw-agent-large-frame",
                                        "type": "agentMessage",
                                        "text": "x" * 49000000,
                                    }
                                ],
                            }
                        ],
                    }
                },
            }
        )
    elif method == "thread/read" and (message.get("params") or {}).get(
        "threadId"
    ) == "raw-thread-not-loaded":
        send(
            {
                "id": request_id,
                "error": {
                    "code": -32600,
                    "message": "thread not loaded: raw-thread-not-loaded",
                },
            }
        )
    elif method == "thread/read":
        send(
            {
                "id": request_id,
                "result": {
                    "thread": {
                        "id": "raw-thread-from-stdio",
                        "name": "Stdio thread",
                        "preview": "Second answer",
                        "status": {"type": "notLoaded"},
                        "createdAt": 1784419200,
                        "updatedAt": 1784419203,
                        "turns": [
                            {
                                "id": "raw-turn-history-one",
                                "status": "completed",
                                "startedAt": 1784419200,
                                "completedAt": 1784419201,
                                "items": [
                                    {
                                        "id": "raw-item-user-one",
                                        "type": "userMessage",
                                        "clientId": "client-history-one",
                                        "content": [
                                            {"type": "text", "text": "First question"}
                                        ],
                                    },
                                    {
                                        "id": "raw-item-agent-one",
                                        "type": "agentMessage",
                                        "text": "First answer" + (" x" * 40000),
                                    },
                                ],
                            },
                            {
                                "id": "raw-turn-history-two",
                                "status": "completed",
                                "startedAt": 1784419202,
                                "completedAt": 1784419203,
                                "items": [
                                    {
                                        "id": "raw-item-user-two",
                                        "type": "userMessage",
                                        "clientId": "client-history-two",
                                        "content": [
                                            {"type": "text", "text": "Second question"}
                                        ],
                                    },
                                    {
                                        "id": "raw-item-agent-two",
                                        "type": "agentMessage",
                                        "text": "Second answer",
                                    },
                                ],
                            },
                        ],
                    }
                },
            }
        )
    else:
        send(
            {
                "id": request_id,
                "error": {"code": -32601, "message": "not implemented"},
            }
        )
