"""Compatibility alias for public device OAuth (including legacy test patches)."""

import sys

from nagents.provider import device_auth
from nagents.provider.device_auth import CLIENT_ID as CLIENT_ID
from nagents.provider.device_auth import ISSUER as ISSUER
from nagents.provider.device_auth import DeviceAuthorization as DeviceAuthorization
from nagents.provider.device_auth import OpenAIAuth as OpenAIAuth
from nagents.provider.device_auth import OpenAIAuthError as OpenAIAuthError
from nagents.provider.device_auth import _Tokens as _Tokens
from nagents.provider.device_auth import _tokens as _tokens

# Re-exports alone break callers patching ISSUER, monotonic or other helpers.
# Both import paths must refer to the same implementation module/globals.
sys.modules[__name__] = device_auth
