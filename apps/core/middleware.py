from django.utils.cache import add_never_cache_headers


class PrivateResponseMiddleware:
    """Never let a reverse proxy cache workspace, authentication or admin responses."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith(("/api/", "/admin/")):
            directives = {
                part.strip().lower() for part in response.get("Cache-Control", "").split(",")
            }
            if not {"private", "no-store"}.issubset(directives):
                add_never_cache_headers(response)
            response["CDN-Cache-Control"] = "no-store"
            response["Vercel-CDN-Cache-Control"] = "no-store"
        return response
