import os


def oauth_availability(_request):
    return {
        "google_oauth_ready": bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()),
        "microsoft_oauth_ready": bool(
            os.environ.get("MICROSOFT_OAUTH_CLIENT_ID", "").strip()
        ),
    }
