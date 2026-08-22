"""obsalt — self-hosted call analytics and quality for AI voice agents.

Core ships no providers. Discover plugins via the ``obsalt.plugins`` entry-point
group and the public contracts in ``obsalt.plugin``.
"""

from obsalt._version import PLUGIN_API_VERSION, SUPPORTED_PLUGIN_API, __version__
from obsalt.domain.enums import (
    HangupParty,
    HangupReason,
    MeasurementPlacement,
    PipelineArchitecture,
    Provenance,
    SignalCoverageStatus,
    Speaker,
    TimelineFidelity,
)
from obsalt.domain.identity import call_id_for
from obsalt.plugin.host import PluginHost

__all__ = [
    "PLUGIN_API_VERSION",
    "SUPPORTED_PLUGIN_API",
    "HangupParty",
    "HangupReason",
    "MeasurementPlacement",
    "PipelineArchitecture",
    "PluginHost",
    "Provenance",
    "SignalCoverageStatus",
    "Speaker",
    "TimelineFidelity",
    "__version__",
    "call_id_for",
]
