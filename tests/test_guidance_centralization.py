"""Contract tests for centralized guidance strings (#113)."""

import inspect

import groksito_discord.llm.client as llm_client
import groksito_discord.llm.prompt_builder as pb
import groksito_discord.llm.tools as tools_mod
from groksito_discord.llm.llm_utils import _infer_tools_set_name
from groksito_discord.llm.tools import log_tool_selection
from groksito_discord.media import audio_handler, image_handler, video_handler


class TestDirectDeliveryCentralization:
    def test_success_strings_defined_in_prompt_builder(self):
        expected = {
            "DIRECT_DELIVERY_SUCCESS_IMAGE",
            "DIRECT_DELIVERY_SUCCESS_EDIT",
            "DIRECT_DELIVERY_SUCCESS_VIDEO",
            "DIRECT_DELIVERY_SUCCESS_AUDIO",
            "DIRECT_DELIVERY_SUCCESS_POLICY_BLOCK",
            "DIRECT_DELIVERY_DETECTOR_PHRASES",
            "TOOL_RESULT_REPLY_SENT",
        }
        assert expected <= set(dir(pb))

    def test_media_handlers_import_success_strings_from_prompt_builder(self):
        assert image_handler.DIRECT_DELIVERY_SUCCESS_IMAGE == pb.DIRECT_DELIVERY_SUCCESS_IMAGE
        assert image_handler.DIRECT_DELIVERY_SUCCESS_EDIT == pb.DIRECT_DELIVERY_SUCCESS_EDIT
        assert image_handler.DIRECT_DELIVERY_SUCCESS_POLICY_BLOCK == pb.DIRECT_DELIVERY_SUCCESS_POLICY_BLOCK
        assert video_handler.DIRECT_DELIVERY_SUCCESS_VIDEO == pb.DIRECT_DELIVERY_SUCCESS_VIDEO
        assert audio_handler.DIRECT_DELIVERY_SUCCESS_AUDIO == pb.DIRECT_DELIVERY_SUCCESS_AUDIO

    def test_detector_phrases_cover_all_success_strings(self):
        for s in (
            pb.DIRECT_DELIVERY_SUCCESS_IMAGE,
            pb.DIRECT_DELIVERY_SUCCESS_EDIT,
            pb.DIRECT_DELIVERY_SUCCESS_VIDEO,
            pb.DIRECT_DELIVERY_SUCCESS_AUDIO,
            pb.DIRECT_DELIVERY_SUCCESS_POLICY_BLOCK,
            pb.TOOL_RESULT_REPLY_SENT,
        ):
            lowered = s.lower()
            assert any(p in lowered for p in pb.DIRECT_DELIVERY_DETECTOR_PHRASES), s

    def test_client_uses_prompt_builder_detector_phrases(self):
        assert llm_client._DIRECT_DELIVERY_SUCCESS_PHRASES is pb.DIRECT_DELIVERY_DETECTOR_PHRASES


class TestToolDescriptionCentralization:
    def test_generate_image_schemas_use_prompt_builder(self):
        full = tools_mod._generate_image_schema()["description"]
        tiny = tools_mod._generate_image_schema_tiny()["description"]
        assert pb.IMAGE_PERMISSIVE_RULE_FULL in full
        assert pb.IMAGE_PERMISSIVE_RULE_TINY in tiny

    def test_edit_image_schema_uses_prompt_builder(self):
        assert tools_mod._edit_image_schema()["description"] == pb.EDIT_IMAGE_TOOL_DESCRIPTION

    def test_video_schemas_use_prompt_builder(self):
        full = video_handler._generate_video_schema()["description"]
        tiny = video_handler._generate_video_schema_tiny()["description"]
        assert pb.VIDEO_TOOL_DESCRIPTION_FULL in full
        assert pb.VIDEO_TOOL_DESCRIPTION_TINY in tiny


class TestToolSetNameSingleSource:
    def test_infer_custom_tools_set_name_matches_legacy_helper(self):
        cases = [
            ("normal", False, False, "normal"),
            ("casual", False, False, "casual-none"),
            ("minimal", False, False, "minimal-core"),
            ("rich", True, False, "rich"),
            ("normal", True, True, "continuation-visual"),
            ("normal", False, True, "continuation-minimal"),
        ]
        for need, visual, cont, expected in cases:
            assert pb.infer_custom_tools_set_name(need, visual, cont) == expected
            assert _infer_tools_set_name(need, visual, cont) == expected

    def test_log_tool_selection_uses_infer_custom_tools_set_name(self):
        source = inspect.getsource(log_tool_selection)
        assert "infer_custom_tools_set_name" in source