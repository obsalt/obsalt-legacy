"""obsalt — self-hosted call analytics and quality for AI voice agents.

Core ships no providers. Install obsalt-vapi, obsalt-retell, or another plugin.
Plugins are trusted, operator-installed code discovered via the obsalt.plugins
entry point. See docs/plugins.md.
"""

from obsalt._version import PLUGIN_API_VERSION, __version__
from obsalt.otel.conventions import setup_tracing
from obsalt.plugin.host import discover_plugins
from obsalt.session import VoiceCall

__all__ = [
    "PLUGIN_API_VERSION",
    "VoiceCall",
    "__version__",
    "discover_plugins",
    "setup_tracing",
]
