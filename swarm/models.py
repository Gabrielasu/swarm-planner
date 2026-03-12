"""Model routing — which agent gets which model.

Supports two backends:
  - api_key: Direct Anthropic API calls
  - opencode: Shell out to `opencode run` (uses Claude subscription)
"""

import json
import os
import subprocess
import sys
from enum import Enum

from pydantic import BaseModel

from .config import load_config


class ModelTier(str, Enum):
    FRONTIER = "frontier"  # Opus — architecture, adversary, decisions
    CODING = "coding"  # Sonnet — contracts, sequencing
    FAST = "fast"  # Haiku — compression, summaries


# Timeouts per tier (seconds)
TIER_TIMEOUTS = {
    ModelTier.FRONTIER: 900,  # 15 minutes — Opus is slow but thorough
    ModelTier.CODING: 600,    # 10 minutes — Sonnet with large payloads
    ModelTier.FAST: 300,      # 5 minutes — Haiku/fast tasks
}


def _get_model_map() -> dict:
    """Build model map from config."""
    cfg = load_config()
    return {
        ModelTier.FRONTIER: cfg["frontier_model"],
        ModelTier.CODING: cfg["coding_model"],
        ModelTier.FAST: cfg["fast_model"],
    }


def call_model(
    context: dict, tier: ModelTier, response_format=None
) -> str | dict | list | BaseModel:
    """Call the appropriate model for this tier.

    Routes to either the Anthropic API or OpenCode based on config.
    """
    cfg = load_config()

    if cfg["auth_method"] == "opencode":
        text = _call_via_opencode(context, tier, response_format)
    else:
        text = _call_via_api(context, tier, response_format, cfg["api_key"])

    if response_format:
        return _parse_structured(text, response_format)
    return text


# -- Anthropic API backend ----------------------------------------------------


def _call_via_api(
    context: dict, tier: ModelTier, response_format, api_key: str
) -> str:
    """Call Anthropic API directly with an API key."""
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    model_map = _get_model_map()
    model = model_map[tier]

    system_prompt = context["system"]
    user_message = context["user"]

    if response_format:
        schema_instruction = _build_schema_instruction(response_format)
        system_prompt += "\n\n" + schema_instruction

    response = client.messages.create(
        model=model,
        max_tokens=16000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    return response.content[0].text


# -- OpenCode backend ---------------------------------------------------------


def _call_via_opencode(
    context: dict, tier: ModelTier, response_format
) -> str:
    """Call a model via `opencode run` subprocess.

    Uses the user's Claude subscription through OpenCode's auth.
    Writes the prompt to a temp file to avoid ARG_MAX limits.
    Uses --format json for reliable output parsing.
    """
    model_map = _get_model_map()
    model = model_map[tier]

    # OpenCode expects provider/model format
    if "/" not in model:
        model = f"anthropic/{model}"

    system_prompt = context["system"]
    user_message = context["user"]

    if response_format:
        schema_instruction = _build_schema_instruction(response_format)
        system_prompt += "\n\n" + schema_instruction

    # Combine system + user into a single message for opencode
    combined = (
        f"<system_instructions>\n{system_prompt}\n</system_instructions>\n\n"
        f"{user_message}"
    )

    timeout = TIER_TIMEOUTS.get(tier, 300)
    env = {**os.environ, "OPENCODE_PERMISSION": '{"permission":"allow"}'}

    # Pass prompt via stdin to avoid ARG_MAX limits and --file tool-use issues
    try:
        result = subprocess.run(
            [
                "opencode", "run",
                "--model", model,
                "--format", "json",
            ],
            input=combined,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(
            f"\n  Error: OpenCode call timed out after {timeout}s.",
            file=sys.stderr,
        )
        sys.exit(1)
    except FileNotFoundError:
        print(
            "\n  Error: 'opencode' not found. Is it installed?",
            file=sys.stderr,
        )
        print("  Install: https://opencode.ai", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        stderr = result.stderr.strip()
        print(
            f"\n  Error: OpenCode returned exit code {result.returncode}",
            file=sys.stderr,
        )
        if stderr:
            print(f"  {stderr[:500]}", file=sys.stderr)
        sys.exit(1)

    # Parse JSON-lines output, extract only text parts
    return _parse_opencode_json_output(result.stdout)


def _parse_opencode_json_output(raw_output: str) -> str:
    """Extract text content from OpenCode's --format json output.

    Each line is a JSON event. We collect all "type":"text" events
    and concatenate their .part.text fields.
    """
    text_parts = []
    event_types_seen = set()
    for line in raw_output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # skip non-JSON lines (startup messages, etc.)
        etype = event.get("type", "unknown")
        event_types_seen.add(etype)
        if etype == "text":
            part = event.get("part", {})
            text = part.get("text", "")
            if text:
                text_parts.append(text)

    if not text_parts:
        # Show all event types seen for debugging
        preview = raw_output[:1000] if raw_output else "(empty)"
        raise RuntimeError(
            f"No text output from OpenCode.\n"
            f"  Event types seen: {event_types_seen}\n"
            f"  Raw output:\n{preview}"
        )

    return "".join(text_parts)


# -- Shared utilities ---------------------------------------------------------


def _build_schema_instruction(response_format) -> str:
    """Generate JSON schema instruction from Pydantic model."""
    if hasattr(response_format, "model_json_schema"):
        schema = json.dumps(response_format.model_json_schema(), indent=2)
    elif hasattr(response_format, "__origin__"):
        args = getattr(response_format, "__args__", ())
        if args and hasattr(args[0], "model_json_schema"):
            item_schema = args[0].model_json_schema()
            schema = json.dumps(
                {"type": "array", "items": item_schema}, indent=2
            )
        else:
            schema = str(response_format)
    else:
        schema = str(response_format)

    return (
        "RESPOND ONLY WITH VALID JSON matching this schema. "
        "No preamble, no markdown fences, no explanation.\n\n"
        f"Schema:\n{schema}"
    )


def _extract_json_string(text: str) -> str:
    """Extract JSON from model output, handling various wrapping formats.

    Tries multiple strategies:
      1. Raw text as-is
      2. Strip markdown code fences (```json ... ```)
      3. Find the outermost { } or [ ] bracket pair
    """
    cleaned = text.strip()

    # Strategy 1: try raw
    if cleaned and cleaned[0] in "{[":
        try:
            json.loads(cleaned)
            return cleaned
        except json.JSONDecodeError:
            pass

    # Strategy 2: strip markdown fences
    # Handle ```json\n...\n``` or ```\n...\n```
    import re
    fence_match = re.search(
        r"```(?:json|JSON)?\s*\n(.*?)```", cleaned, re.DOTALL
    )
    if fence_match:
        candidate = fence_match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    # Strategy 3: find outermost brackets
    # Look for first { or [ and matching last } or ]
    first_brace = cleaned.find("{")
    first_bracket = cleaned.find("[")

    if first_brace == -1 and first_bracket == -1:
        raise json.JSONDecodeError(
            f"No JSON found in model output. "
            f"First 200 chars: {cleaned[:200]!r}",
            cleaned, 0
        )

    # Pick whichever comes first
    if first_brace == -1:
        start, open_char, close_char = first_bracket, "[", "]"
    elif first_bracket == -1:
        start, open_char, close_char = first_brace, "{", "}"
    else:
        if first_brace < first_bracket:
            start, open_char, close_char = first_brace, "{", "}"
        else:
            start, open_char, close_char = first_bracket, "[", "]"

    # Find the matching close bracket by counting nesting
    depth = 0
    in_string = False
    escape_next = False
    end = -1
    for i in range(start, len(cleaned)):
        c = cleaned[i]
        if escape_next:
            escape_next = False
            continue
        if c == "\\":
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == open_char:
            depth += 1
        elif c == close_char:
            depth -= 1
            if depth == 0:
                end = i
                break

    if end == -1:
        # JSON was truncated (output token limit). Try to repair it.
        candidate = _repair_truncated_json(cleaned[start:])
        return candidate

    candidate = cleaned[start : end + 1]
    # Final validation
    json.loads(candidate)  # raises JSONDecodeError if still invalid
    return candidate


def _repair_truncated_json(text: str) -> str:
    """Repair JSON truncated by output token limits.

    Walks the string tracking open brackets and string state,
    trims to the last cleanly-closed value, then closes all
    remaining open brackets.
    """
    import re

    # Walk the text, track bracket stack and string state
    stack = []       # stack of closing chars needed: } or ]
    in_string = False
    escape_next = False
    last_clean = 0   # position after the last complete key:value or array element

    for i, c in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if c == "\\" and in_string:
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            stack.append('}')
        elif c == '[':
            stack.append(']')
        elif c in '}]' and stack:
            stack.pop()
            last_clean = i + 1
        elif c == ',':
            last_clean = i

    if not stack:
        # Fully closed — shouldn't get here, but try parsing anyway
        return text

    # Trim to last clean position (avoids partial strings/values)
    trimmed = text[:last_clean].rstrip().rstrip(",")

    # If we're inside a string at the trim point, close it
    # Re-check string state up to the trim point
    in_string = False
    escape_next = False
    for c in trimmed:
        if escape_next:
            escape_next = False
            continue
        if c == "\\" and in_string:
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
    if in_string:
        trimmed += '"'

    # Recount what brackets are still open after trimming
    stack = []
    in_string = False
    escape_next = False
    for c in trimmed:
        if escape_next:
            escape_next = False
            continue
        if c == "\\" and in_string:
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            stack.append('}')
        elif c == '[':
            stack.append(']')
        elif c in '}]' and stack:
            stack.pop()

    # Close all open brackets
    closing = "".join(reversed(stack))
    repaired = trimmed + closing

    # Validate
    try:
        json.loads(repaired)
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"JSON repair failed. Original was {len(text)} chars, "
            f"trimmed to {len(trimmed)}, "
            f"added closers '{closing}'. "
            f"Inner error: {e.msg}",
            repaired, e.pos
        )

    print(
        f"   (!) JSON was truncated ({len(text)} chars). "
        f"Repaired by closing {len(stack)} open brackets. "
        f"Some data at the end may be missing.",
        file=sys.stderr,
    )
    return repaired


def _parse_structured(text: str, response_format):
    """Parse model response into structured format."""
    try:
        cleaned = _extract_json_string(text)
    except json.JSONDecodeError:
        # Show useful diagnostic on failure
        preview = text[:300] if len(text) > 300 else text
        suffix = text[-200:] if len(text) > 500 else ""
        raise RuntimeError(
            f"Failed to extract JSON from model output "
            f"({len(text)} chars).\n"
            f"  Start: {preview!r}\n"
            f"  End: {suffix!r}" if suffix else
            f"Failed to extract JSON from model output "
            f"({len(text)} chars).\n"
            f"  Content: {preview!r}"
        )

    parsed = json.loads(cleaned)

    if hasattr(response_format, "model_validate"):
        return response_format.model_validate(parsed)

    if hasattr(response_format, "__origin__"):
        args = getattr(response_format, "__args__", ())
        if args and hasattr(args[0], "model_validate") and isinstance(parsed, list):
            return [args[0].model_validate(item) for item in parsed]

    return parsed
