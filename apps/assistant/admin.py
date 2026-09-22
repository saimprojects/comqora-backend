from django import forms
from django.contrib import admin, messages
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters

from .credentials import encrypt_key
from .models import AssistantModel, Connection, Conversation, ProposedAction, Turn
from .provider import ProviderError, api_key, request_json


class ConnectionForm(forms.ModelForm):
    api_key = forms.CharField(
        label="Fazita API key",
        required=False,
        max_length=4096,
        widget=forms.PasswordInput(
            render_value=False, attrs={"autocomplete": "new-password", "spellcheck": "false"}
        ),
        help_text="Paste your Fazita API key and save. Stored encrypted. Leave blank to keep the saved key; it is never shown again.",
    )
    clear_api_key = forms.BooleanField(
        required=False,
        label="Remove saved API key",
        help_text="Clears the saved key. If an environment fallback exists, that key will be used instead.",
    )

    class Meta:
        model = Connection
        fields = [
            "name",
            "api_key",
            "clear_api_key",
            "enabled",
            "daily_workspace_turn_limit",
            "key_environment_variable",
        ]

    def clean(self):
        values = super().clean()
        key = values.get("api_key", "")
        if key and values.get("clear_api_key"):
            raise forms.ValidationError("Choose either replace or remove the key, not both.")
        if key and (any(c.isspace() for c in key) or not key.isascii()):
            self.add_error("api_key", "Paste the API key only, without spaces or a Bearer prefix.")
        return values

    def save(self, commit=True):
        connection = super().save(commit=False)
        if self.cleaned_data.get("clear_api_key"):
            connection.encrypted_api_key = ""
        elif self.cleaned_data.get("api_key"):
            connection.encrypted_api_key = encrypt_key(self.cleaned_data["api_key"])
        if commit:
            connection.save()
            self.save_m2m()
        return connection


class ModelInline(admin.TabularInline):
    model = AssistantModel
    extra = 0
    fields = ["name", "model_id", "protocol", "enabled", "priority", "max_output_tokens"]


@admin.register(Connection)
class ConnectionAdmin(admin.ModelAdmin):
    form = ConnectionForm
    list_display = ["name", "enabled", "key_configured", "daily_workspace_turn_limit"]
    inlines = [ModelInline]
    actions = ["discover_models"]
    readonly_fields = ["key_configured", "supported_endpoints"]
    fieldsets = [
        (
            None,
            {
                "fields": [
                    "name",
                    "api_key",
                    "key_configured",
                    "enabled",
                    "daily_workspace_turn_limit",
                ]
            },
        ),
        (
            "Advanced",
            {
                "classes": ["collapse"],
                "fields": ["clear_api_key", "key_environment_variable", "supported_endpoints"],
            },
        ),
    ]

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    @method_decorator(sensitive_post_parameters("api_key"))
    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        return super().changeform_view(request, object_id, form_url, extra_context)

    @admin.display(boolean=True)
    def key_configured(self, obj):
        return bool(api_key(obj))

    @admin.display(description="Fazita endpoints")
    def supported_endpoints(self, obj):
        return "GET /v1/models · POST /v1/responses · POST /v1/chat/completions · POST /v1/messages. Business assistant uses text + tools; image generation is not a business-data endpoint."

    @admin.action(
        description="Discover Fazita models (adds disabled entries; choose protocol before enabling)"
    )
    def discover_models(self, request, queryset):
        for connection in queryset[:3]:
            try:
                data = request_json(connection, "models")
                rows = data.get("data", [])
                if not isinstance(rows, list):
                    raise ProviderError("Model catalogue returned an unsupported format.")
                count = 0
                for row in rows[:500]:
                    identifier = row.get("id") if isinstance(row, dict) else None
                    if not isinstance(identifier, str) or not 1 <= len(identifier) <= 150:
                        continue
                    if not connection.models.filter(model_id=identifier).exists():
                        AssistantModel.objects.create(
                            connection=connection,
                            name=identifier[:100],
                            model_id=identifier,
                            enabled=False,
                        )
                        count += 1
                self.message_user(
                    request,
                    f"{connection.name}: {count} disabled models added. Select the correct protocol and test tool support before enabling.",
                )
            except ProviderError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)


@admin.register(AssistantModel)
class AssistantModelAdmin(admin.ModelAdmin):
    list_display = ["name", "model_id", "connection", "protocol", "enabled", "priority"]
    list_filter = ["enabled", "protocol", "connection"]
    search_fields = ["name", "model_id"]
    list_editable = ["enabled", "priority"]


class ReadOnlyAIAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Conversation)
class ConversationAdmin(ReadOnlyAIAdmin):
    list_display = ["title", "workspace", "user", "updated_at", "archived"]
    list_filter = ["workspace", "archived"]


@admin.register(Turn)
class TurnAdmin(ReadOnlyAIAdmin):
    list_display = ["id", "workspace", "model", "status", "created_at"]
    list_filter = ["status", "model", "workspace"]


@admin.register(ProposedAction)
class ActionAdmin(ReadOnlyAIAdmin):
    list_display = ["id", "workspace", "kind", "status", "created_at"]
    list_filter = ["status", "kind", "workspace"]
