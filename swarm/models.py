"""Model routing — which agent gets which model.

Supports two backends:
  - api_key: Direct Anthropic API calls
  - opencode: Shell out to `opencode run` (uses Claude subscription)
"""

import json
import os
import re
import subprocess
import sys
import time
from enum import Enum

from pydantic import BaseModel

from .config import load_config


class ModelTier(str, Enum):
    FRONTIER = "frontier"  # Opus — architecture, adversary, decisions
    CODING = "coding"  # Sonnet — contracts, sequencing
    FAST = "fast"  # Haiku — compression, summaries


class ModelTimeoutError(Exception):
    """Raised when a model call times out. Callers can catch and retry."""

    def __init__(self, tier: ModelTier, timeout: int, message: str = ""):
        self.tier = tier
        self.timeout = timeout
        super().__init__(
            message
            or f"Model call ({tier.value}) timed out after {timeout}s"
        )


class ModelCallError(Exception):
    """Raised when a model call fails (non-timeout). Callers can retry."""

    def __init__(self, message: str, returncode: int = -1):
        self.returncode = returncode
        super().__init__(message)


# Timeouts per tier (seconds)
TIER_TIMEOUTS = {
    ModelTier.FRONTIER: 900,  # 15 minutes — Opus is slow but thorough
    ModelTier.CODING: 600,    # 10 minutes — Sonnet with large payloads
    ModelTier.FAST: 300,      # 5 minutes — Haiku/fast tasks
}

# Retry configuration
MAX_RETRIES = 2          # retry up to 2 times (3 attempts total)
RETRY_BACKOFF_BASE = 10  # seconds between retries


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
    """Call the appropriate model for this tier, with automatic retry.

    Routes to either the Anthropic API or OpenCode based on config.
    Retries on timeout or transient errors with exponential backoff.
    """
    cfg = load_config()
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            if cfg["auth_method"] == "opencode":
                text = _call_via_opencode(context, tier, response_format)
            else:
                text = _call_via_api(
                    context, tier, response_format, cfg["api_key"]
                )

            if response_format:
                return _parse_structured(text, response_format)
            return text

        except ModelTimeoutError as e:
            last_error = e
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_BASE * (attempt + 1)
                print(
                    f"\n   (!) Timeout on attempt {attempt + 1}/{MAX_RETRIES + 1}. "
                    f"Retrying in {wait}s...",
                    file=sys.stderr,
                )
                time.sleep(wait)
            else:
                raise

        except ModelCallError as e:
            last_error = e
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_BASE * (attempt + 1)
                print(
                    f"\n   (!) Error on attempt {attempt + 1}/{MAX_RETRIES + 1}: "
                    f"{e}. Retrying in {wait}s...",
                    file=sys.stderr,
                )
                time.sleep(wait)
            else:
                raise

    # Should not reach here, but just in case
    if last_error is not None:
        raise last_error
    raise ModelCallError("All retry attempts exhausted")


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
        raise ModelTimeoutError(
            tier=tier,
            timeout=timeout,
            message=f"OpenCode call timed out after {timeout}s",
        )
    except FileNotFoundError:
        print(
            "\n  Error: 'opencode' not found. Is it installed?",
            file=sys.stderr,
        )
        print("  Install: https://opencode.ai", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise ModelCallError(
            f"OpenCode returned exit code {result.returncode}"
            + (f": {stderr[:500]}" if stderr else ""),
            returncode=result.returncode,
        )

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
            # Try sanitizing before moving on
            try:
                sanitized = _sanitize_json(cleaned)
                json.loads(sanitized)
                print(
                    "   (i) JSON had minor issues (trailing commas / "
                    "control chars), auto-repaired.",
                    file=sys.stderr,
                )
                return sanitized
            except json.JSONDecodeError:
                pass

    # Strategy 2: strip markdown fences
    # Handle ```json\n...\n``` or ```\n...\n```
    fence_match = re.search(
        r"```(?:json|JSON)?\s*\n(.*?)```", cleaned, re.DOTALL
    )
    if fence_match:
        candidate = fence_match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            try:
                sanitized = _sanitize_json(candidate)
                json.loads(sanitized)
                print(
                    "   (i) JSON had minor issues, auto-repaired.",
                    file=sys.stderr,
                )
                return sanitized
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
    # Final validation — try raw first, then sanitized
    try:
        json.loads(candidate)
        return candidate
    except json.JSONDecodeError:
        sanitized = _sanitize_json(candidate)
        try:
            json.loads(sanitized)
            print(
                "   (i) JSON had minor issues, auto-repaired.",
                file=sys.stderr,
            )
            return sanitized
        except json.JSONDecodeError:
            raise  # propagate the error from sanitized attempt


def _repair_truncated_json(text: str) -> str:
    """Repair JSON truncated by output token limits.

    Walks the string tracking open brackets and string state,
    trims to the last cleanly-closed value, then closes all
    remaining open brackets.
    """
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


def _sanitize_json(text: str) -> str:
    """Fix common JSON issues in LLM output.

    Handles:
      - Trailing commas before } or ]
      - Literal control characters inside string values (newlines, tabs, etc.)
    """
    # 1. Remove trailing commas before closing brackets
    text = re.sub(r",(\s*[}\]])", r"\1", text)

    # 2. Fix literal control characters inside JSON string values.
    #    JSON spec requires control chars (U+0000–U+001F) to be escaped.
    #    LLMs often emit literal newlines/tabs inside string values.
    def _fix_control_chars(match: re.Match) -> str:
        s = match.group(0)
        parts: list[str] = []
        i = 0
        while i < len(s):
            c = s[i]
            # Preserve already-escaped sequences
            if c == "\\" and i + 1 < len(s):
                parts.append(s[i : i + 2])
                i += 2
                continue
            code = ord(c)
            if code < 0x20:
                if c == "\n":
                    parts.append("\\n")
                elif c == "\r":
                    parts.append("\\r")
                elif c == "\t":
                    parts.append("\\t")
                else:
                    parts.append(f"\\u{code:04x}")
            else:
                parts.append(c)
            i += 1
        return "".join(parts)

    # Match JSON string literals (handles escaped quotes inside)
    text = re.sub(
        r'"(?:[^"\\]|\\.)*"', _fix_control_chars, text, flags=re.DOTALL
    )

    return text


def _parse_structured(text: str, response_format):
    """Parse model response into structured format.

    Raises ModelCallError on parse failure so the retry loop can retry.
    """
    try:
        cleaned = _extract_json_string(text)
    except json.JSONDecodeError as je:
        # Show useful diagnostic on failure — raise ModelCallError so
        # the retry loop in call_model() can retry the whole call
        preview = text[:300] if len(text) > 300 else text
        suffix = text[-200:] if len(text) > 500 else ""
        msg = (
            f"Failed to extract JSON from model output "
            f"({len(text)} chars).\n"
            f"  Parse error: {je.msg} at pos {je.pos}\n"
            f"  Start: {preview!r}"
        )
        if suffix:
            msg += f"\n  End: {suffix!r}"
        raise ModelCallError(msg)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ModelCallError(
            f"JSON decode failed after extraction ({len(cleaned)} chars): {e}"
        )

    try:
        if hasattr(response_format, "model_validate"):
            return response_format.model_validate(parsed)

        if hasattr(response_format, "__origin__"):
            args = getattr(response_format, "__args__", ())
            if args and hasattr(args[0], "model_validate") and isinstance(parsed, list):
                return [args[0].model_validate(item) for item in parsed]

        return parsed
    except Exception as e:
        raise ModelCallError(
            f"Schema validation failed: {e}"
        )
