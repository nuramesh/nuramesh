"""The one place an outbound TLS client context is made (ND483).

Certificate and hostname verification stay on; the protocol floor is TLS 1.2. Stdlib only.
"""

import ssl


def default_client_context() -> ssl.SSLContext:
    """A verifying client context (CERT_REQUIRED, hostname check) that refuses below TLS 1.2."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context
