"""Compatibility marker for ngn bundles; the runtime itself is `nagents engine`.

ngn releases up to 0.100.46 ran this package as a per-run adapter. It now only
answers the bundle probe those releases use during `ngn update`.
"""

__version__ = "0.100.52"
