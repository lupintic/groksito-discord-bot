"""Regression tests for reply-chain context injection (#109).

When users reply 2-3x in a chain to Groksito, prior bot output must be labeled
as the bot's own previous messages — not as a generic [R:...] referent the model
may copy verbatim into the visible reply.
"""

from groksito_discord.llm.llm_input import _build_dynamic_referenced_context_block


class TestBotSelfReferenceContext:
    """Bot-authored referenced messages use unmistakable self-reference labeling."""

    def test_reply_to_bot_avoids_r_prefix_and_adds_do_not_repeat_note(self):
        prior_bot_text = "Here is a detailed explanation about quantum computing and its applications."
        block = _build_dynamic_referenced_context_block(
            referenced_context={
                "author": "Groksito",
                "content": prior_bot_text,
                "is_bot": True,
            },
            reply_chain_contexts=None,
            is_reply_to_bot=True,
            is_mentioned=False,
        )

        assert "[R:Groksito]" not in block
        assert "my previous response" in block.lower()
        assert "[My last message]" in block
        assert prior_bot_text[:50] in block
        assert "do not repeat" in block.lower()

    def test_mention_reply_to_user_keeps_r_prefix(self):
        user_text = "Check out this YouTube link about cats"
        block = _build_dynamic_referenced_context_block(
            referenced_context={
                "author": "Alice",
                "content": user_text,
                "is_bot": False,
            },
            reply_chain_contexts=None,
            is_reply_to_bot=False,
            is_mentioned=True,
        )

        assert "[R:Alice]" in block
        assert "my previous response" not in block.lower()
        assert user_text in block

    def test_chain_ancestor_bot_uses_earlier_message_label(self):
        bot_earlier = "I already explained the first part of the answer."
        block = _build_dynamic_referenced_context_block(
            referenced_context={
                "author": "Groksito",
                "content": "My most recent reply to the user.",
                "is_bot": True,
            },
            reply_chain_contexts=[
                {"author": "Groksito", "content": "My most recent reply to the user.", "is_bot": True},
                {"author": "Groksito", "content": bot_earlier, "is_bot": True},
                {"author": "Bob", "content": "Original user question", "is_bot": False},
            ],
            is_reply_to_bot=True,
            is_mentioned=False,
        )

        assert "[Chain ancestor 1 by Groksito]" not in block
        assert "[My earlier message 1]" in block
        assert bot_earlier[:30] in block
        assert "do not repeat" in block.lower()

    def test_chain_ancestor_user_keeps_chain_label(self):
        user_ancestor = "Here is the YouTube video I shared earlier"
        block = _build_dynamic_referenced_context_block(
            referenced_context={
                "author": "Groksito",
                "content": "Sure, let me summarize that video.",
                "is_bot": True,
            },
            reply_chain_contexts=[
                {"author": "Groksito", "content": "Sure, let me summarize that video.", "is_bot": True},
                {"author": "Carol", "content": user_ancestor, "is_bot": False},
            ],
            is_reply_to_bot=True,
            is_mentioned=False,
        )

        assert "[Chain ancestor 1 by Carol]" in block
        assert user_ancestor in block

    def test_non_addressed_turn_injects_nothing(self):
        block = _build_dynamic_referenced_context_block(
            referenced_context={"author": "Groksito", "content": "ignored", "is_bot": True},
            reply_chain_contexts=None,
            is_reply_to_bot=False,
            is_mentioned=False,
        )
        assert block == ""