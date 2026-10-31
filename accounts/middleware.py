from django.shortcuts import redirect
from django.urls import reverse


class StaffTwoFactorMiddleware:
    """Every account must enrol in two-factor authentication before doing anything else (correction 11: for any
    user, not only staff; the class keeps its name for the settings)."""

    ALLOWED_PREFIXES = ("/account/2fa/setup/", "/logout/", "/static/", "/i18n/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and user.requires_2fa
            and not user.totp_enabled
            and not request.path_info.startswith(self.ALLOWED_PREFIXES)
        ):
            return redirect(reverse("totp_setup"))
        return self.get_response(request)


class ListReturnMiddleware:
    """A form posted from a detail shown under a list carries ``back`` (the list with that row selected): after the
    action the person returns there instead of to the object's own page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.method == "POST" and 300 <= response.status_code < 400 and request.headers.get("X-Partial") == "1":
            # a form sent in the background (the igw dialogs): the page it returns to, as a partial (with the messages)
            response["Location"] = with_partial(response.get("Location", ""))
            return response
        if request.method == "POST" and 300 <= response.status_code < 400:
            back = request.POST.get("back", "")
            if back.startswith("/") and not back.startswith("//") and "\\" not in back:
                response["Location"] = keep_dialog(back, response.get("Location", ""))
        return response


DIALOG_PARAMS = ("igw", "provision")  # a dialog the action goes back to (the tenant page's igw dialogs, Provision)


def keep_dialog(back, location):
    """The list URL with the dialog the view wanted open (owner 2026-10-29: Read config must stay in the dialog);
    olist.js passes it on to the selected row's details."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    want = [(k, v) for k, v in parse_qsl(urlsplit(location).query) if k in DIALOG_PARAMS]
    if not want:
        return back
    u = urlsplit(back)
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k not in DIALOG_PARAMS] + want
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q), u.fragment))


def with_partial(location):
    """The address with partial=1 (no fragment): the page's content without the frame, its messages first."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    u = urlsplit(location)
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "partial"] + [("partial", "1")]
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q), ""))
