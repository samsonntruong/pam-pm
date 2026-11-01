"""Single place where model request messages are assembled. TODO: implement."""

# Fixed order: system prompt, tool definitions, project context,
# then conversation in order, then the volatile block LAST.


def build_messages(role, project_context, conversation, volatile):
    """TODO: assemble messages in fixed order. See META_API_NOTES section 3."""
    raise NotImplementedError
