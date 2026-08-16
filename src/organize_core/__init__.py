"""organize-core — the engine behind the KMS organize stage.

One program, two entry modes (spec 10 §1): the ``organize`` CLI and the
long-running JSON-RPC server (``organize serve``). All behavior lives here;
clients (the Neovim plugin, Claude agents, systemd) are thin.

SHARED FILE — only the architect/integrator edits this module.
"""

__version__ = "0.1.0"

# JSON-RPC handshake version (spec 10 §2). Clients refuse a major-version
# mismatch with a clear message.
API_VERSION = 1
