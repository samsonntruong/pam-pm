"""Model client behind one interface. TODO: implement OpenAI-compatible chat."""

# See META_API_NOTES section 3 for the prompt caching playbook:
# every request goes through agents.prompts.builder.build_messages.


class ModelClient:
    """TODO: chat completions with tools, structured output, image input."""
