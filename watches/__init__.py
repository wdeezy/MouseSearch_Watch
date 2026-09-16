"""
Watches: saved MAM searches that run on a schedule and auto-grab new releases.

- ``store``  : sqlite persistence (watch definitions, dedupe table, event history)
- ``runner`` : search -> regex filter -> dedupe -> cap -> add-to-client pipeline
- ``routes`` : Quart blueprint exposing the JSON API under ``/api/watches``
"""
