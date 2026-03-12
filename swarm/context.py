"""Context assembly for each agent.

Each agent gets exactly the tokens it needs and nothing more.
"""

from pathlib import Path


PROMPT_DIR = Path(__file__).parent / "prompts"


def assemble_context(prompt_file: str, variables: dict) -> dict:
    """Build a context packet for an agent call.

    Returns {"system": str, "user": str} ready for the model.

    The prompt file contains the agent's identity and instructions
    (becomes the system prompt). The variables contain the specific
    input for this call (becomes the user message).
    """
    # Read the prompt template
    prompt_path = PROMPT_DIR / Path(prompt_file).name
    prompt_template = prompt_path.read_text()

    # The system prompt is the agent's identity and instructions
    system = prompt_template

    # The user message is the assembled variables
    user_parts = []
    for key, value in variables.items():
        if value:
            user_parts.append(f"<{key}>\n{value}\n</{key}>")

    user = "\n\n".join(user_parts)

    return {"system": system, "user": user}
