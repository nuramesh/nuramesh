"""The API's own first-owner setup page, `GET /setup` (SA-0.10; #950).

`nuramesh serve` prints `http://<host>:<port>/setup#token=<token>`; this is the page behind it. It
is self-contained (no app, no bundle, nothing from another origin) and follows the
`headers.swagger_ui_page` precedent: one inline script and one inline style, each allowed by its
sha256 in the page's own Content-Security-Policy, never by `'unsafe-inline'`.

What the script does, and nothing else: read the token from the fragment (a browser never sends a
fragment to a server), drop the fragment from the address bar and the history entry at once, keep
the token in a variable only, and send it in the JSON body of `POST /v1/auth/setup/redeem`. It
shows fixed texts and never the server's message (a 403 reads only the problem's `code`, to tell
a foreign-origin refusal from a bad link). Without a fragment the page's own text stays
and no form is shown (the page is fragment-only; a headless person uses the app's page).
"""

import base64
import hashlib
import html
from typing import Final

from starlette.responses import HTMLResponse

SETUP_PATH: Final = "/setup"
REDEEM_PATH: Final = "/v1/auth/setup/redeem"

TEXT_NO_TOKEN: Final = (
    "Open the link that nuramesh serve printed, or run nuramesh auth new-setup-token --team <dir> "
    "and paste the token on the app's setup page."
)
TEXT_CREATED: Final = "Owner created. Sign in from the app."
TEXT_INVALID: Final = (
    "This setup link is not valid or has expired. Restart serve or run "
    "nuramesh auth new-setup-token."
)
TEXT_FOREIGN: Final = (
    "This request came from another site, so it was refused. "
    "Open the link that nuramesh serve printed."
)
TEXT_CHECK: Final = "Check the email address and the password."
TEXT_WAIT: Final = "Too many attempts. Wait and try again."
TEXT_OTHER: Final = "Something went wrong. Nothing was changed."
TEXT_ASK: Final = "Choose the email address and the password of the first owner."

SETUP_STYLE: Final = """
body { font-family: system-ui, sans-serif; margin: 0; background: #f6f7f9; color: #14181f; }
main { max-width: 26rem; margin: 4rem auto; padding: 0 1rem; }
h1 { font-size: 1.4rem; }
label { display: block; margin: 1rem 0 0.25rem; }
input { box-sizing: border-box; width: 100%; padding: 0.5rem; font: inherit; }
button { margin-top: 1.25rem; padding: 0.5rem 1rem; font: inherit; }
"""

SETUP_SCRIPT: Final = f"""
(function () {{
  "use strict";
  var TEXT = {{
    ask: "{TEXT_ASK}",
    created: "{TEXT_CREATED}",
    invalid: "{TEXT_INVALID}",
    foreign: "{TEXT_FOREIGN}",
    check: "{TEXT_CHECK}",
    wait: "{TEXT_WAIT}",
    other: "{TEXT_OTHER}"
  }};
  var REFUSALS = {{ 422: TEXT.check, 429: TEXT.wait }};
  var PREFIX = "#token=";
  var fragment = window.location.hash;
  var token = fragment.indexOf(PREFIX) === 0 ? fragment.slice(PREFIX.length) : "";
  fragment = "";
  window.history.replaceState(null, "", window.location.pathname);
  if (!token) {{
    return;
  }}
  var message = document.getElementById("setup-message");
  var form = document.getElementById("setup-form");
  var email = document.getElementById("setup-email");
  var password = document.getElementById("setup-password");
  var button = document.getElementById("setup-submit");
  var busy = false;
  message.textContent = TEXT.ask;
  form.hidden = false;
  form.addEventListener("submit", function (event) {{
    event.preventDefault();
    if (busy || !token) {{
      return;
    }}
    busy = true;
    button.disabled = true;
    fetch("{REDEEM_PATH}", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ token: token, email: email.value, password: password.value }}),
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer"
    }}).then(function (response) {{
      var status = response.status;
      if (status === 201) {{
        token = "";
        form.reset();
        form.hidden = true;
        message.textContent = TEXT.created;
        return;
      }}
      if (status === 403) {{
        return response.json().then(function (body) {{
          var foreign = body && body.code === "foreign_origin";
          message.textContent = foreign ? TEXT.foreign : TEXT.invalid;
        }}, function () {{
          message.textContent = TEXT.invalid;
        }});
      }}
      message.textContent = REFUSALS[status] || TEXT.other;
    }}, function () {{
      message.textContent = TEXT.other;
    }}).then(function () {{
      busy = false;
      button.disabled = false;
    }});
  }});
}})();
"""

SETUP_PAGE: Final = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>NuraMesh setup</title>
<style>{SETUP_STYLE}</style>
</head>
<body>
<main>
<h1>Set up NuraMesh</h1>
<p id="setup-message" role="status">{html.escape(TEXT_NO_TOKEN)}</p>
<form id="setup-form" hidden novalidate>
<label for="setup-email">Email address</label>
<input id="setup-email" name="email" type="email" autocomplete="username" required>
<label for="setup-password">Password</label>
<input id="setup-password" name="password" type="password" autocomplete="new-password" required>
<button id="setup-submit" type="submit">Create the owner</button>
</form>
</main>
<script>{SETUP_SCRIPT}</script>
</body>
</html>
"""


def _source(text: str) -> str:
    """The CSP source (`'sha256-...'`) that allows exactly this inline text."""
    return f"'sha256-{base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()}'"


SETUP_CSP: Final = (
    f"default-src 'none'; script-src {_source(SETUP_SCRIPT)}; style-src {_source(SETUP_STYLE)}; "
    "connect-src 'self'; form-action 'none'; frame-ancestors 'none'"
)
"""Exactly the policy of SA-0.10; `headers.SecurityHeaders` puts it on the page's 200 only."""


def setup_page() -> HTMLResponse:
    """The page (`text/html; charset=utf-8`); the security headers come from the middleware."""
    return HTMLResponse(SETUP_PAGE)
