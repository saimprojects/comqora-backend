from django.urls import path

from . import api

urlpatterns = [
    path("config/", api.Config.as_view()),
    path("conversations/", api.Conversations.as_view()),
    path("conversations/<uuid:pk>/", api.ConversationDetail.as_view()),
    path("conversations/<uuid:pk>/send/", api.Send.as_view()),
    path("conversations/<uuid:pk>/cancel/", api.CancelResearch.as_view()),
    path("actions/<uuid:pk>/", api.ActionDecision.as_view()),
]
