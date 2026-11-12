"""The suites. Each module registers its checks with @check, in run order, and the modules are
imported in the order their groups run. A new suite is a new module here, plus a switch under
suites.* in the chart values and config.py.

connectivity (always on): rhcl (Connectivity Link status), gateway, mesh. Rate-limit checks wait for their window to
expire first, so they don't count requests from the checks before them.
ingress, pki, workloads, storage, observability, argocd, metallb (off by default): create test objects in the scratch namespace.
"""

from . import rhcl, gateway, mesh  # noqa: F401, I001 - order matters: it is the run order
from . import ingress, pki, workloads, storage, observability, argocd, metallb  # noqa: F401
