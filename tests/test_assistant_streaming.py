import io
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, override_settings

from apps.assistant.models import AssistantModel
from apps.assistant.provider import ProviderError, Session, read_response_stream, request_json


def event(value):
    return (
        b"event: ignored\r\ndata: " + json.dumps(value, ensure_ascii=False).encode() + b"\r\n\r\n"
    )


def completed(output=None):
    return {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": output or [],
            "usage": {"input_tokens": 7, "output_tokens": 3},
        },
    }


class Response(io.BytesIO):
    def __init__(self, value, content_type="text/event-stream", chunk_size=8192):
        super().__init__(value)
        self.headers = {"Content-Type": content_type}
        self.chunk_size = chunk_size

    def read1(self, size=-1):
        return super().read1(min(size, self.chunk_size))


@override_settings(FAZITA_API_KEY="synthetic-stream-key")
class StreamingTests(SimpleTestCase):
    @patch("apps.assistant.provider.request_json")
    def test_provider_default_omits_token_parameter_and_messages_requires_one(self, request):
        self.model.max_output_tokens = None
        for protocol, response in [
            ("responses", {"output": []}),
            ("chat", {"choices": [{"message": {"content": "OK"}}]}),
        ]:
            self.model.protocol = protocol
            request.return_value = response
            Session(self.model, "Policy", [], []).ask()
            self.assertNotIn("max_output_tokens", request.call_args.args[2])
            self.assertNotIn("max_completion_tokens", request.call_args.args[2])
        self.model.protocol = "messages"
        request.reset_mock()
        with self.assertRaises(ProviderError):
            Session(self.model, "Policy", [], []).ask()
        request.assert_not_called()

    def test_blank_budget_admin_validation(self):
        from django.forms import modelform_factory

        form_class = modelform_factory(AssistantModel, fields=["protocol", "max_output_tokens"])
        self.assertTrue(
            form_class(data={"protocol": "responses", "max_output_tokens": ""}).is_valid()
        )
        form = form_class(data={"protocol": "messages", "max_output_tokens": ""})
        self.assertFalse(form.is_valid())
        self.assertIn("max_output_tokens", form.errors)

    def test_token_exhaustion_is_distinguished_without_exposing_raw_errors(self):
        for reason, expected in [
            ("max_output_tokens", "output-token limit"),
            ("content_filter", "content filtering"),
        ]:
            with self.assertRaisesMessage(ProviderError, expected):
                self.parse(
                    event(
                        {
                            "type": "response.incomplete",
                            "response": {"incomplete_details": {"reason": reason}},
                        }
                    )
                )
        with self.assertRaisesMessage(ProviderError, "server_error"):
            self.parse(
                event(
                    {
                        "type": "response.failed",
                        "response": {"error": {"code": "server_error", "message": "secret"}},
                    }
                )
            )

    def test_output_budget_default_and_admin_bounds(self):
        field = AssistantModel._meta.get_field("max_output_tokens")
        self.assertEqual(AssistantModel().max_output_tokens, 8192)
        for value in [512, 8192, 32768]:
            self.assertEqual(field.clean(value, None), value)
        for value in [511, 32769]:
            with self.assertRaises(ValidationError):
                field.clean(value, None)

    @patch("apps.assistant.provider.request_json")
    def test_larger_budget_is_forwarded_for_all_protocols(self, request):
        self.model.max_output_tokens = 8192
        for protocol, parameter, response in [
            ("responses", "max_output_tokens", {"output": []}),
            ("chat", "max_completion_tokens", {"choices": [{"message": {"content": "OK"}}]}),
            ("messages", "max_tokens", {"content": [{"type": "text", "text": "OK"}]}),
        ]:
            self.model.protocol = protocol
            request.return_value = response
            Session(self.model, "Policy", [], []).ask()
            self.assertEqual(request.call_args.args[2][parameter], 8192)

    def setUp(self):
        self.connection = SimpleNamespace(
            encrypted_api_key="", key_environment_variable="FAZITA_API_KEY"
        )
        self.model = SimpleNamespace(
            model_id="synthetic",
            protocol="responses",
            connection=self.connection,
            max_output_tokens=2048,
        )

    def parse(self, value, chunk_size=8192):
        return read_response_stream(Response(value, chunk_size=chunk_size), time.monotonic() + 60)

    def test_fragmented_utf8_comments_and_gateway_metadata(self):
        result = completed(
            [{"type": "message", "content": [{"type": "output_text", "text": "سلام — OK"}]}]
        )
        stream = (
            b": heartbeat\r\n\r\n"
            + event({"type": "codex.rate_limits"})
            + event({"type": "response.output_text.delta", "delta": "ignored partial"})
            + event(result)
        )
        self.assertEqual(self.parse(stream, 3), result["response"])

    def test_multiline_event(self):
        raw = (
            b'data: {"type":"response.completed",\n'
            + b'data: "response":{"status":"completed","output":[]}}\n\n'
        )
        self.assertEqual(self.parse(raw)["status"], "completed")

    def test_partial_tool_and_done_marker_are_not_completions(self):
        partial = event(
            {
                "type": "response.output_item.done",
                "item": {"type": "function_call", "name": "prepare_action", "arguments": "{}"},
            }
        )
        for stream in [partial, partial + b"data: [DONE]\n\n", event({"type": "response.created"})]:
            with self.assertRaises(ProviderError):
                self.parse(stream)

    def test_failed_incomplete_and_invalid_completion_are_rejected(self):
        for value in [
            {"type": "error", "message": "provider secret"},
            {"type": "response.failed", "response": {"error": "provider secret"}},
            {"type": "response.incomplete", "response": {"output": [{"type": "function_call"}]}},
            {"type": "response.completed", "response": {"status": "in_progress", "output": []}},
        ]:
            with self.assertRaises(ProviderError) as error:
                self.parse(event(value))
            self.assertNotIn("provider secret", str(error.exception))

    def test_response_size_and_elapsed_time_are_bounded(self):
        with (
            patch("apps.assistant.provider.MAX_RESPONSE_BYTES", 10),
            self.assertRaises(ProviderError),
        ):
            self.parse(event(completed()))
        with self.assertRaises(ProviderError):
            read_response_stream(Response(event(completed())), time.monotonic() - 1)

    @patch("apps.assistant.provider.build_opener")
    def test_actual_session_accepts_a_130_second_stream_and_preserves_tool_output(self, opener):
        clock = [100.0]
        output = [
            {"type": "reasoning", "encrypted_content": "opaque"},
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "business_overview",
                "arguments": "{}",
            },
        ]

        def open_response(request, timeout):
            self.assertGreater(timeout, 15)
            self.assertLessEqual(timeout, 300)
            self.assertTrue(json.loads(request.data)["stream"])
            self.assertEqual(request.get_header("Accept"), "text/event-stream")
            clock[0] += 130
            return Response(event(completed(output)))

        opener.return_value.open.side_effect = open_response
        with patch("apps.assistant.provider.time.monotonic", side_effect=lambda: clock[0]):
            session = Session(self.model, "Policy", [{"role": "user", "content": "Overview"}], [])
            _, calls, usage = session.ask()
        self.assertEqual(calls[0]["name"], "business_overview")
        session.add_results([("c1", '{"count":2}')])
        self.assertEqual(session.messages[-1]["type"], "function_call_output")
        self.assertEqual(session.messages[-3]["encrypted_content"], "opaque")
        self.assertEqual(usage["output_tokens"], 3)

    @patch("apps.assistant.provider.build_opener")
    def test_json_fallback_and_short_catalogue_timeout(self, opener):
        opener.return_value.open.return_value = Response(b'{"data": []}', "application/json")
        self.assertEqual(request_json(self.connection, "models"), {"data": []})
        self.assertLessEqual(opener.return_value.open.call_args.kwargs["timeout"], 15)
        opener.return_value.open.return_value = Response(
            json.dumps(completed()["response"]).encode(), "application/json"
        )
        self.assertEqual(
            request_json(self.connection, "responses", {"stream": True})["status"], "completed"
        )

    @patch("apps.assistant.provider.build_opener")
    def test_remaining_turn_budget_caps_network_timeout(self, opener):
        opener.return_value.open.return_value = Response(event(completed()))
        with patch("apps.assistant.provider.time.monotonic", return_value=100):
            request_json(self.connection, "responses", {"stream": True}, deadline=112)
        self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 12)

    @patch("apps.assistant.provider.build_opener")
    def test_timeouts_are_not_automatically_retried(self, opener):
        opener.return_value.open.side_effect = TimeoutError()
        with self.assertRaises(ProviderError):
            request_json(self.connection, "responses", {"stream": True})
        self.assertEqual(opener.return_value.open.call_count, 1)

    @patch("apps.assistant.provider.request_json")
    def test_incomplete_json_cannot_smuggle_partial_tool_calls(self, request):
        request.return_value = {
            "status": "incomplete",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "prepare_action",
                    "arguments": "{}",
                }
            ],
        }
        with self.assertRaises(ProviderError):
            Session(self.model, "Policy", [], []).ask()
