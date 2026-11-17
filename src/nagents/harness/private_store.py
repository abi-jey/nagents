"""Compatibility alias for the public protected-store filesystem boundary."""

import sys

from nagents import protected_store
from nagents.protected_store import ProtectedFileLock as ProtectedFileLock
from nagents.protected_store import ProtectedFileStore as ProtectedFileStore
from nagents.protected_store import ProtectedStoreError as ProtectedStoreError

# Preserve module-global monkeypatches as well as class/exception identity.
sys.modules[__name__] = protected_store
