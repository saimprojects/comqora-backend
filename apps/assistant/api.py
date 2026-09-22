from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.models import Workspace

from .actions import decide, review_details
from .models import AssistantModel, Conversation, ProposedAction, Turn
from .provider import UNAVAILABLE, api_key
from .worker import ACTIVE_STATUSES, expire_interrupted

NOTICE = "Your AI Manager can read all business data in this workspace, including historical orders, customers, purchases, inventory, campaigns, expenses, bank/wallet ledgers, settlements and messaging records. Relevant data is sent to Fazita and its model providers to answer your questions. No separate read permission is required. Other workspaces and credentials are never accessible. Changes require confirmation; normal write permissions still apply. AI can make mistakes—check its sources."


class AssistantAccess(permissions.BasePermission):
    message = "An active Ultra AI subscription is required."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user.is_authenticated and user.is_active and user.workspace_id and user.has_ai_access
        )


class PrivateView(APIView):
    permission_classes = [AssistantAccess]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response


def action_data(action):
    return {
        "id": str(action.pk),
        "kind": action.kind,
        "payload": action.payload,
        "review": review_details(action),
        "status": "EXPIRED"
        if action.status == "PENDING" and action.expires_at <= timezone.now()
        else action.status,
        "result": action.result,
        "expires_at": action.expires_at,
    }


def turn_data(turn):
    return {
        "id": str(turn.pk),
        "request_key": str(turn.request_key),
        "question": turn.question,
        "answer": turn.answer,
        "status": turn.status,
        "error": UNAVAILABLE if turn.status == "ERROR" else "",
        "sources": turn.sources,
        "steps": turn.steps,
        "usage": turn.usage,
        "model_name": turn.model.name,
        "created_at": turn.created_at,
        "actions": [action_data(a) for a in turn.actions.all()],
    }


def owned(request):
    return Conversation.objects.filter(workspace=request.user.workspace, user=request.user)


class Config(PrivateView):
    def get(self, request):
        models = AssistantModel.objects.filter(
            enabled=True, connection__enabled=True
        ).select_related("connection")
        available = [{"id": m.pk, "name": m.name} for m in models if api_key(m.connection)]
        return Response(
            {
                "name": request.user.workspace.name + "'s Manager",
                "notice": NOTICE,
                "models": available,
                "ready": bool(available),
                "capabilities": [
                    "All-time workspace search",
                    "Business & profit analysis",
                    "Bank, wallet & settlements",
                    "Inventory & purchases",
                    "Customer and order history",
                    "Confirmed action proposals",
                ],
            }
        )


class Conversations(PrivateView):
    def get(self, request):
        page = serializers.IntegerField(min_value=1, max_value=10000).run_validation(
            request.query_params.get("page", 1)
        )
        qs = owned(request).filter(archived=False).order_by("-updated_at", "-id")
        return Response(
            {
                "count": qs.count(),
                "results": list(
                    qs.values("id", "title", "updated_at")[(page - 1) * 30 : page * 30]
                ),
                "next_page": page + 1 if qs.count() > page * 30 else None,
            }
        )

    def post(self, request):
        with transaction.atomic():
            Workspace.objects.select_for_update().get(pk=request.user.workspace_id)
            if owned(request).filter(created_at__date=timezone.localdate()).count() >= 100:
                return Response(
                    {
                        "detail": "Daily conversation limit reached. Continue an existing conversation."
                    },
                    status=429,
                )
            obj = Conversation.objects.create(workspace=request.user.workspace, user=request.user)
        return Response({"id": obj.pk, "title": obj.title}, status=201)


class ConversationDetail(PrivateView):
    def get(self, request, pk):
        conversation = get_object_or_404(owned(request), pk=pk)
        # A crashed/terminated web worker must not strand the UI indefinitely.
        expire_interrupted(conversation.turns.all())
        limit = 30
        page = serializers.IntegerField(min_value=1, max_value=10000).run_validation(
            request.query_params.get("page", 1)
        )
        turns = (
            conversation.turns.select_related("model")
            .prefetch_related("actions")
            .order_by("-created_at", "-id")
        )
        selected = list(turns[(page - 1) * limit : page * limit])
        return Response(
            {
                "id": conversation.pk,
                "title": conversation.title,
                "turns": [turn_data(t) for t in reversed(selected)],
                "older_page": page + 1 if turns.count() > page * limit else None,
            }
        )

    def patch(self, request, pk):
        obj = get_object_or_404(owned(request), pk=pk)
        obj.archived = serializers.BooleanField().run_validation(request.data.get("archived"))
        obj.save(update_fields=["archived", "updated_at"])
        return Response({"id": obj.pk, "archived": obj.archived})


class SendInput(serializers.Serializer):
    question = serializers.CharField(max_length=8000, min_length=1)
    request_key = serializers.UUIDField()
    model_id = serializers.IntegerField(required=False, min_value=1)


class Send(PrivateView):
    def post(self, request, pk):
        serializer = SendInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        with transaction.atomic():
            Workspace.objects.select_for_update().get(pk=request.user.workspace_id)
            conversation = get_object_or_404(owned(request), pk=pk, archived=False)
            old = conversation.turns.filter(request_key=values["request_key"]).first()
            if old:
                if old.question != values["question"]:
                    return Response(
                        {"detail": "This request ID belongs to a different message."}, status=409
                    )
                return Response(
                    turn_data(old), status=202 if old.status in ACTIVE_STATUSES else 200
                )
            expire_interrupted(Turn.objects.filter(workspace=request.user.workspace))
            running = Turn.objects.filter(
                workspace=request.user.workspace, status__in=ACTIVE_STATUSES
            )
            if running.filter(conversation=conversation).exists() or running.count() >= 3:
                return Response(
                    {
                        "detail": "The assistant is already investigating. Wait for the current request to finish."
                    },
                    status=409,
                )
            models = AssistantModel.objects.filter(
                enabled=True, connection__enabled=True
            ).select_related("connection")
            if values.get("model_id"):
                models = models.filter(pk=values["model_id"])
            model = next((m for m in models if api_key(m.connection)), None)
            if not model:
                return Response(
                    {"detail": UNAVAILABLE},
                    status=503,
                )
            if (
                Turn.objects.filter(
                    workspace=request.user.workspace, created_at__date=timezone.localdate()
                ).count()
                >= model.connection.daily_workspace_turn_limit
            ):
                return Response(
                    {"detail": "Your workspace's daily AI message limit has been reached."},
                    status=429,
                )
            turn = Turn.objects.create(
                workspace=request.user.workspace,
                conversation=conversation,
                model=model,
                question=values["question"],
                request_key=values["request_key"],
                status="QUEUED",
            )
            if conversation.title == "New conversation":
                conversation.title = values["question"][:100]
            conversation.save(update_fields=["title", "updated_at"])
        return Response(turn_data(turn), status=202)


class CancelResearch(PrivateView):
    def post(self, request, pk):
        conversation = get_object_or_404(owned(request), pk=pk)
        with transaction.atomic():
            turn = conversation.turns.select_for_update().filter(status__in=ACTIVE_STATUSES).first()
            if turn:
                turn.status = "CANCELLED"
                turn.processing_token = None
                turn.finished_at = timezone.now()
                turn.save(update_fields=["status", "processing_token", "finished_at", "updated_at"])
                turn.actions.filter(status="PENDING").update(
                    status="CANCELLED", decided_at=timezone.now()
                )
        return Response({"status": "CANCELLED" if turn else "IDLE"})


class ActionDecision(PrivateView):
    def post(self, request, pk):
        decision = serializers.ChoiceField(choices=["confirm", "cancel"]).run_validation(
            request.data.get("decision")
        )
        # Hide IDs outside this user's conversation even within the same workspace.
        get_object_or_404(
            ProposedAction,
            pk=pk,
            workspace=request.user.workspace,
            turn__conversation__user=request.user,
        )
        return Response(action_data(decide(request, pk, decision)))
