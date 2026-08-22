# Plugins

Core ships no providers. Plugins are separately installable packages registered on the
`obsalt.plugins` entry-point group. First-party providers use that **same** group.

Plugins are **trusted, operator-installed code**. They are pinned, inventoried, and
receive only the credentials needed for their capability. Loading isolates version
mismatches and ordinary exceptions; that is not a security sandbox. An arbitrary
wheel can still read process memory. Tenant-installable plugins need a future
out-of-process runtime.

```python
from obsalt.plugin import Capability, PLUGIN_API_VERSION

class MyPlugin:
    API_VERSION = PLUGIN_API_VERSION
    name = "acme"
    display_name = "Acme Voice"
    capabilities = frozenset({Capability.WEBHOOK_SOURCE, Capability.AUTHENTICATION})
```

`obsalt-testkit` ships inheritable conformance tests. Skipping a required test needs
an explicit marker plus a written reason.

`StreamSource` is declared in v2 with an example implementation so Deepgram is
additive and does not require a core change.
