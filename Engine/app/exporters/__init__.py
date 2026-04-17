"""Per-backend exporter implementations.

Each module in this package exposes an ``add_items`` function with the
signature::

    add_items(items: list[dict], **kwargs) -> tuple[int, list[str]]

returning ``(success_count, error_messages)``. The dispatcher in
``app.exporter`` calls these with the kwargs each backend needs.
"""
