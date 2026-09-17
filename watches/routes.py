"""JSON API for watches, mounted at ``/api/watches``."""
from __future__ import annotations

from quart import Blueprint, jsonify, request

from . import runner, store

watches_bp = Blueprint("watches", __name__, url_prefix="/api/watches")


async def _json_body() -> dict:
    data = await request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _error(message: str, status: int = 400):
    return jsonify({"error": message}), status


@watches_bp.route("/status", methods=["GET"])
async def watches_status():
    return jsonify({
        "enabled": bool(runner.deps.config("WATCHES_ENABLED", False)),
        "tick_minutes": runner.deps.config("WATCHES_TICK_MINUTES", 5),
        "min_interval_minutes": store.MIN_INTERVAL_MINUTES,
        "watch_count": len(store.list_watches()),
    })


@watches_bp.route("", methods=["GET"])
@watches_bp.route("/", methods=["GET"])
async def list_watches():
    return jsonify({"watches": store.list_watches()})


@watches_bp.route("", methods=["POST"])
@watches_bp.route("/", methods=["POST"])
async def create_watch():
    data = await _json_body()
    data.pop("id", None)
    try:
        watch = store.upsert_watch(data)
    except store.WatchValidationError as exc:
        return _error(str(exc))
    return jsonify({"watch": watch, "message": f"Watch '{watch['name']}' saved."}), 201


@watches_bp.route("/test", methods=["POST"])
async def test_unsaved_watch():
    """Dry-run an unsaved watch definition (used by 'Save as watch' previews)."""
    data = await _json_body()
    data.pop("id", None)
    try:
        watch = store.normalize_watch(data)
    except store.WatchValidationError as exc:
        return _error(str(exc))
    summary = await runner.run_watch(watch, dry_run=True)
    return jsonify(summary)


@watches_bp.route("/<int:watch_id>", methods=["GET"])
async def get_watch(watch_id: int):
    watch = store.get_watch(watch_id)
    if watch is None:
        return _error("watch not found", 404)
    return jsonify({"watch": watch})


@watches_bp.route("/<int:watch_id>", methods=["PUT", "PATCH"])
async def update_watch(watch_id: int):
    if store.get_watch(watch_id) is None:
        return _error("watch not found", 404)
    data = await _json_body()
    data["id"] = watch_id
    try:
        watch = store.upsert_watch(data)
    except store.WatchValidationError as exc:
        return _error(str(exc))
    return jsonify({"watch": watch, "message": f"Watch '{watch['name']}' updated."})


@watches_bp.route("/<int:watch_id>", methods=["DELETE"])
async def delete_watch(watch_id: int):
    if not store.delete_watch(watch_id):
        return _error("watch not found", 404)
    return jsonify({"message": "Watch deleted."})


@watches_bp.route("/<int:watch_id>/test", methods=["POST"])
async def test_watch(watch_id: int):
    watch = store.get_watch(watch_id)
    if watch is None:
        return _error("watch not found", 404)
    summary = await runner.run_watch(watch, dry_run=True)
    return jsonify(summary)


@watches_bp.route("/<int:watch_id>/run", methods=["POST"])
async def run_watch_now(watch_id: int):
    watch = store.get_watch(watch_id)
    if watch is None:
        return _error("watch not found", 404)
    summary = await runner.run_watch(watch, dry_run=False)
    summary["watch"] = store.get_watch(watch_id)
    return jsonify(summary)


@watches_bp.route("/<int:watch_id>/events", methods=["GET"])
async def watch_events(watch_id: int):
    if store.get_watch(watch_id) is None:
        return _error("watch not found", 404)
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"events": store.events_for(watch_id, limit)})


@watches_bp.route("/<int:watch_id>/seen", methods=["GET"])
async def watch_seen_list(watch_id: int):
    if store.get_watch(watch_id) is None:
        return _error("watch not found", 404)
    return jsonify({"seen": store.list_seen(watch_id)})


@watches_bp.route("/<int:watch_id>/seen", methods=["POST"])
async def watch_mark_seen(watch_id: int):
    """
    Mark the given results as seen without grabbing them. Body:
    ``{"items": [{"id": "...", "title": "..."}, ...]}`` (or ``{"ids": [...]}``).
    Used by the test panel's "Mark all current as seen" button so the first live
    run does not grab the back-catalog.
    """
    if store.get_watch(watch_id) is None:
        return _error("watch not found", 404)
    data = await _json_body()
    items = data.get("items")
    if not isinstance(items, list):
        items = data.get("ids") if isinstance(data.get("ids"), list) else []
    count = store.mark_many_seen(watch_id, items, grabbed=False)
    if count:
        store.log_event(watch_id, "skipped_seen", detail=f"{count} result(s) marked seen manually")
    return jsonify({"message": f"Marked {count} result(s) as seen.", "count": count})


@watches_bp.route("/<int:watch_id>/seen", methods=["DELETE"])
async def watch_clear_seen(watch_id: int):
    if store.get_watch(watch_id) is None:
        return _error("watch not found", 404)
    data = await _json_body()
    mam_id = data.get("id") or request.args.get("id")
    count = store.clear_seen(watch_id, mam_id)
    return jsonify({"message": f"Cleared {count} seen record(s).", "count": count})
