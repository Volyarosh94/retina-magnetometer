"""Vulture dead-code whitelist.

Read by offworldlabs/ops check-dead-code.sh. Every name here is reported dead
by vulture but is referenced by something vulture cannot see. Real dead code
is deleted, not listed.
"""
# ruff: noqa: B018, F821
# B018 — bare-name expressions are how vulture whitelists work.
# F821 — these names are defined in other modules; only vulture reads this file.

_ = type("_", (), {})()

# socketserver calls handle() on each connection.
#   rm3100_sim/server.py
_.handle
# socketserver.ThreadingMixIn / TCPServer class attributes.
#   rm3100_sim/server.py
_.allow_reuse_address
_.daemon_threads
# Flask error handler, registered by decorator on the blueprint.
#   retina_magnetometer/web/__init__.py
bad_request
