"""ANNA: Adaptive Neural Network Assistant.

Top-level package. The runtime entrypoint is :func:`anna.__main__.main`. The
package is organized into the following subpackages:

* :mod:`anna.core`: identity files, eviction, persistence on disk.
* :mod:`anna.runtime`: supervisor, watchdog, router, per-conversation worker.
* :mod:`anna.transports`: ChannelAdapter ABC plus Slack and Telegram implementations.
* :mod:`anna.agents`: sub-agent persona registry and auto-hire flow.
* :mod:`anna.skills`: skill-as-persona-modifier registry.
* :mod:`anna.vault`: checkpoint, transcript, and audit writers.
* :mod:`anna.setup`: interactive setup wizard.
* :mod:`anna.cli`: anna-logs and anna-admin command-line tools.

See the v3 buildout plan for the architecture rationale.
"""

__version__ = "0.1.0"
