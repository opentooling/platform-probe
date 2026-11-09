"""The suite. Each module registers its checks with @check, in run order, and the modules are
imported in the order their groups run. A new suite is a new module here.

Rate-limit checks wait for their window to expire first, so they don't count requests from
the checks before them.
"""

from . import platform, gateway, mesh  # noqa: F401, I001 - order matters: it is the run order
