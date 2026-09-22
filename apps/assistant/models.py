import uuid

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models

from apps.core.models import TenantModel


class Connection(models.Model):
    name = models.CharField(max_length=80, default="Fazita")
    encrypted_api_key = models.TextField(blank=True, editable=False)
    key_environment_variable = models.CharField(
        max_length=80,
        default="FAZITA_API_KEY",
        validators=[
            RegexValidator(
                r"^FAZITA_[A-Z0-9_]+$",
                "Use a FAZITA_ environment variable name, not the secret itself.",
            )
        ],
        help_text="Optional fallback when no API key is saved above. Most admins can leave this unchanged.",
    )
    enabled = models.BooleanField(default=True)
    daily_workspace_turn_limit = models.PositiveIntegerField(
        default=100, validators=[MinValueValidator(1), MaxValueValidator(10000)]
    )

    class Meta:
        verbose_name = "AI provider connection"

    def __str__(self):
        return self.name


class AssistantModel(models.Model):
    connection = models.ForeignKey(Connection, on_delete=models.PROTECT, related_name="models")
    name = models.CharField(max_length=100, help_text="Friendly name shown in the assistant.")
    model_id = models.CharField(
        max_length=150, help_text="Exact model ID from your Fazita group's Use Key screen."
    )
    protocol = models.CharField(
        max_length=16,
        default="responses",
        choices=[
            ("responses", "Responses · /v1/responses"),
            ("chat", "Chat completions · /v1/chat/completions"),
            ("messages", "Anthropic messages · /v1/messages"),
        ],
    )
    enabled = models.BooleanField(
        default=False,
        help_text="Enable after checking your key/group supports this model and tool calling.",
    )
    priority = models.PositiveIntegerField(
        default=100, help_text="Lowest number is the default model."
    )
    max_output_tokens = models.PositiveIntegerField(
        default=8192,
        null=True,
        blank=True,
        validators=[MinValueValidator(512), MaxValueValidator(32768)],
        help_text="Leave blank for provider default (Responses/Chat): no app token cap is sent; provider limits still apply. Messages requires a number. Higher budgets can increase cost and latency.",
    )

    def clean(self):
        super().clean()
        if self.protocol == "messages" and self.max_output_tokens is None:
            raise ValidationError(
                {"max_output_tokens": "Messages requires an explicit token budget."}
            )

    class Meta:
        ordering = ["priority", "name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "model_id", "protocol"], name="unique_assistant_model"
            )
        ]

    def __str__(self):
        return self.name


class Conversation(TenantModel):
    user = models.ForeignKey("accounts.User", on_delete=models.PROTECT)
    title = models.CharField(max_length=100, default="New conversation")
    archived = models.BooleanField(default=False)


class Turn(TenantModel):
    conversation = models.ForeignKey(Conversation, related_name="turns", on_delete=models.PROTECT)
    model = models.ForeignKey(AssistantModel, on_delete=models.PROTECT)
    request_key = models.UUIDField(default=uuid.uuid4)
    question = models.TextField()
    answer = models.TextField(blank=True)
    status = models.CharField(
        max_length=12,
        default="RUNNING",
        choices=[(x, x.title()) for x in ["QUEUED", "RUNNING", "COMPLETE", "ERROR", "CANCELLED"]],
    )
    error = models.CharField(max_length=300, blank=True)
    sources = models.JSONField(default=list)
    steps = models.JSONField(default=list)
    usage = models.JSONField(default=dict)
    finished_at = models.DateTimeField(null=True)
    processing_token = models.UUIDField(null=True, blank=True, editable=False)

    class Meta(TenantModel.Meta):
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "request_key"], name="unique_assistant_turn_request"
            )
        ]


class ProposedAction(TenantModel):
    turn = models.ForeignKey(Turn, related_name="actions", on_delete=models.PROTECT)
    kind = models.CharField(max_length=40)
    payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=12,
        default="PENDING",
        choices=[(x, x.title()) for x in ["PENDING", "APPLIED", "CANCELLED", "EXPIRED"]],
    )
    result = models.JSONField(default=dict)
    expires_at = models.DateTimeField()
    decided_at = models.DateTimeField(null=True)
