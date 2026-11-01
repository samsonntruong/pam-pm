"""Tool definitions for model calls. Plain literals only, byte stable."""

FINISH_INTERVIEW = {
    "function": {
        "description": "Record the structured interview result for one work item.",
        "name": "finish_interview",
        "parameters": {
            "additionalProperties": False,
            "properties": {
                "blocker": {
                    "description": "What blocks the item, or null when not blocked.",
                    "type": ["string", "null"],
                },
                "blocker_owner": {
                    "description": "Owner of the blocking work, or null.",
                    "type": ["string", "null"],
                },
                "confidence": {
                    "description": "Confidence from 0 to 1.",
                    "type": "number",
                },
                "eta": {
                    "description": "Expected completion date as ISO text, or null.",
                    "type": ["string", "null"],
                },
                "evidence_quote": {
                    "description": "Short verbatim quote backing the status.",
                    "type": "string",
                },
                "remaining": {
                    "description": "Remaining work in hours, or null.",
                    "type": ["number", "null"],
                },
                "status": {
                    "description": "Current status of the item in plain words.",
                    "type": "string",
                },
            },
            "required": ["confidence", "evidence_quote", "status"],
            "type": "object",
        },
    },
    "type": "function",
}
