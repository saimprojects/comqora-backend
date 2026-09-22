from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.assistant.models import AssistantModel, Connection, Conversation, ProposedAction, Turn
from apps.assistant.worker import expire_interrupted, process_turns
from tests.billing_fixtures import paid_workspace


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    FAZITA_API_KEY="synthetic-key",
)
class ResearchJobTests(TransactionTestCase):
    def setUp(self):
        self.ws = paid_workspace(name="Synthetic workspace")
        self.user = User.objects.create_user(
            username="researcher",
            email="research@example.test",
            workspace=self.ws,
            dashboard_access_state="ACTIVE",
        )
        self.model = AssistantModel.objects.create(
            connection=Connection.objects.create(),
            model_id="synthetic",
            enabled=True,
            max_output_tokens=None,
        )
        self.chat = Conversation.objects.create(workspace=self.ws, user=self.user)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = f"/api/assistant/conversations/{self.chat.pk}/"

    def send(self, key=None):
        return self.client.post(
            self.url + "send/",
            {"question": "Synthetic overview", "request_key": str(key or uuid4())},
        )

    @patch("apps.assistant.service.Session.ask")
    def test_send_is_queued_nonblocking_and_idempotent(self, ask):
        key = uuid4()
        first = self.send(key)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.data["status"], "QUEUED")
        self.assertEqual(self.send(key).data["id"], first.data["id"])
        self.assertEqual(Turn.objects.count(), 1)
        self.assertEqual(self.send().status_code, 409)
        ask.assert_not_called()
        ask.return_value = ("Synthetic answer", [], {})
        self.assertEqual(process_turns(), 1)
        self.assertEqual(process_turns(), 0)
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(self.client.get(self.url).data["turns"][0]["status"], "COMPLETE")

    @patch("apps.assistant.service.execute", return_value={"synthetic": True})
    @patch("apps.assistant.service.Session.ask")
    def test_long_research_exceeds_old_time_and_round_limits(self, ask, execute):
        turn_id = self.send().data["id"]
        Turn.objects.filter(pk=turn_id).update(created_at=timezone.now() - timedelta(minutes=30))
        calls = [0]

        def step(*args, **kwargs):
            self.assertNotIn("deadline", kwargs)
            # Polling an old but actively heartbeating turn must not expire it.
            self.assertEqual(self.client.get(self.url).data["turns"][0]["status"], "RUNNING")
            calls[0] += 1
            if calls[0] <= 8:
                return (
                    "",
                    [{"id": str(calls[0]), "name": "workspace_profile", "arguments": "{}"}],
                    {"input_tokens": 10},
                )
            return "Research complete", [], {"output_tokens": 5}

        ask.side_effect = step
        process_turns()
        turn = Turn.objects.get(pk=turn_id)
        self.assertEqual(turn.status, "COMPLETE", turn.error)
        self.assertEqual(len(turn.steps), 8)
        self.assertEqual(turn.usage["provider_requests"], 9)

    @patch("apps.assistant.service.Session.ask")
    def test_cancel_queued_job_never_calls_provider(self, ask):
        self.send()
        self.assertEqual(self.client.post(self.url + "cancel/").data["status"], "CANCELLED")
        self.assertEqual(process_turns(), 0)
        ask.assert_not_called()

    @patch("apps.assistant.service.execute")
    @patch("apps.assistant.service.Session.ask")
    def test_cancel_during_provider_call_fences_late_results_and_proposals(self, ask, execute):
        pk = self.send().data["id"]
        proposal = ProposedAction.objects.create(
            workspace=self.ws,
            turn_id=pk,
            kind="create_expense",
            expires_at=timezone.now() + timedelta(minutes=20),
        )

        def cancel_then_return(*args, **kwargs):
            self.client.post(self.url + "cancel/")
            return "Late answer", [{"id": "x", "name": "prepare_action", "arguments": "{}"}], {}

        ask.side_effect = cancel_then_return
        process_turns()
        turn = Turn.objects.get(pk=pk)
        self.assertEqual(turn.status, "CANCELLED")
        self.assertEqual(turn.answer, "")
        execute.assert_not_called()
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, "CANCELLED")

    def test_only_inactive_running_jobs_expire_and_queued_jobs_survive(self):
        pk = self.send().data["id"]
        Turn.objects.filter(pk=pk).update(updated_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(expire_interrupted(Turn.objects.all()), 0)
        Turn.objects.filter(pk=pk).update(status="RUNNING", processing_token=uuid4())
        self.assertEqual(expire_interrupted(Turn.objects.all()), 1)
        self.assertIsNone(Turn.objects.get(pk=pk).processing_token)
        self.assertEqual(process_turns(), 0)

    @patch("apps.assistant.service.Session.ask")
    def test_access_revocation_before_worker_starts_blocks_provider(self, ask):
        pk = self.send().data["id"]
        self.user.dashboard_access_state = "PENDING"
        self.user.save()
        process_turns()
        ask.assert_not_called()
        self.assertEqual(Turn.objects.get(pk=pk).status, "ERROR")

    def test_cancel_is_private_to_chat_author(self):
        self.send()
        colleague = User.objects.create_user(
            username="colleague",
            email="colleague@example.test",
            workspace=self.ws,
            dashboard_access_state="ACTIVE",
        )
        self.client.force_authenticate(colleague)
        self.assertEqual(self.client.post(self.url + "cancel/").status_code, 404)
        self.assertEqual(Turn.objects.get().status, "QUEUED")
