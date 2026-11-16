"""Tool schema compatibility tests for the Antigravity bridge."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.client import _translate_tools_to_gemini  # noqa: E402


def test_translate_tools_removes_json_schema_keywords_rejected_by_code_assist():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "example_tool",
                "description": "Example",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "duration": {
                            "oneOf": [
                                {"type": "integer", "minimum": 5, "maximum": 20},
                                {"type": "string", "const": "auto"},
                            ],
                            "default": "auto",
                        }
                    },
                    "required": ["duration"],
                },
            },
        }
    ]

    translated = _translate_tools_to_gemini(tools)
    params = translated[0]["functionDeclarations"][0]["parameters"]

    assert "additionalProperties" not in params
    duration = params["properties"]["duration"]
    assert "default" not in duration
    assert "oneOf" not in duration
    assert duration["type"] == "integer"
