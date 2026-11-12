"""Bounded reclamation of dead stage payloads and file-wasted space.

A dead work row must not retain the bulky stage cache
(``bundle``/``proposal``/``review``): the durable record of what happened is
the body-free ``knowledge_failures`` audit row. Bulk reclamation is the single
choke point that enforces it, so a terminal path added later (or written by an
older build) cannot silently restore the unbounded growth that first tripped the
100MB export fuse.

Only ``done``/``obsolete`` rows are swept, except work explicitly retained by
``memory_cutover_entries``. Retired memory-mode work retains its frozen evidence
for cutover verification. A ``failed`` row deliberately keeps
its bundle, because ``delta_operator._frozen_window_reference_parts`` reads the
original frozen window out of it to build an operator completion plan.

No pipeline transition clears these columns: a terminal write keeps every other
column byte-identical so a later audit or CAS can still prove the row itself was
not rewritten (``test_failed_recovery`` pins this). This module is therefore the
single place that reclaims dead payloads -- one choke point that a newly added
write path cannot bypass, instead of the scattered per-path cleanup whose
omission from the delta pipeline caused the 2026-10 regression.

Clearing columns is not enough on its own. SQLite reuses freed pages for new
writes but never lowers the file's high-water mark, and the portable export
copies the whole database file, so a stale high-water mark still trips the
export fuse. A bounded ``VACUUM`` therefore runs *only* when enough free space
has accumulated to be worth the rewrite; otherwise the sweep is a cheap
indexed no-op.
"""
import time

# States whose payload can never be read again. ``done`` clears in its own
# terminal write; ``obsolete`` is the bulk item. ``failed`` is deliberately NOT
# listed: the operator completion path re-derives the original frozen window
# reference parts from a failed row's bundle, so a failed bundle still has a
# live consumer. ``retry``/``budget_wait``/``publication_pending``/``blocked``
# keep their cache so the next attempt resumes without re-calling the model.
TERMINAL_STATES = ('done', 'obsolete')
BULKY_COLUMNS = ('bundle', 'proposal', 'review')

DEFAULT_LIMIT = 200
DEFAULT_VACUUM_FREE_BYTES = 16 * 1024 * 1024


def _positive_int(value, default):
    """A bounded knob: a non-integer or a value <= 0 falls back, never raises."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default


def _page_stats(db):
    page_size = db.execute('PRAGMA page_size').fetchone()[0]
    page_count = db.execute('PRAGMA page_count').fetchone()[0]
    free_pages = db.execute('PRAGMA freelist_count').fetchone()[0]
    return page_size, page_count, free_pages


def reclaim(engine, limit=DEFAULT_LIMIT, vacuum_free_bytes=DEFAULT_VACUUM_FREE_BYTES):
    """Clear terminal stage caches (bounded) and reclaim file space.

    ``limit`` bounds how many rows one call may touch, so a large backlog of
    legacy rows is drained over several cycles instead of blocking the
    maintenance loop. ``vacuum_free_bytes`` is the free-space threshold below
    which a rewrite is not worth it; a negative value disables the vacuum
    (used by callers that want the sweep only).
    """
    limit = _positive_int(limit, DEFAULT_LIMIT)
    if isinstance(vacuum_free_bytes, bool) or not isinstance(vacuum_free_bytes, int):
        vacuum_free_bytes = DEFAULT_VACUUM_FREE_BYTES
    placeholder = ','.join('?' * len(TERMINAL_STATES))
    cleared = 0
    with engine.locked():
        with engine.transaction():
            retained = engine.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_cutover_entries'"
            ).fetchone()
            retention = (" AND NOT EXISTS (SELECT 1 FROM memory_cutover_entries mc"
                         " WHERE mc.table_name='knowledge_work' AND mc.row_id=knowledge_work.id)"
                         if retained else "")
            rows = engine.db.execute(
                ("SELECT id FROM knowledge_work WHERE state IN (%s)"
                 " AND (bundle IS NOT NULL OR proposal IS NOT NULL OR review IS NOT NULL)" % placeholder)
                + retention + " ORDER BY created_at LIMIT ?",
                TERMINAL_STATES + (limit,)).fetchall()
            ids = [row['id'] for row in rows]
            if ids:
                engine.db.execute(
                    "UPDATE knowledge_work SET bundle=NULL,proposal=NULL,review=NULL"
                    " WHERE id IN (%s)" % ','.join('?' * len(ids)), ids)
                cleared = len(ids)
        page_size, page_count, free_pages = _page_stats(engine.db)
        free_bytes = page_size * free_pages
        vacuumed = False
        # VACUUM opens its own transaction and rewrites the file, so it must run
        # outside the sweep transaction, under the same process lock.
        if vacuum_free_bytes >= 0 and free_bytes >= vacuum_free_bytes:
            engine.db.execute('VACUUM')
            vacuumed = True
        page_size, page_count, free_pages = _page_stats(engine.db)
    return {
        'cleared': cleared,
        'vacuumed': vacuumed,
        'free_bytes': page_size * free_pages,
        'bytes': page_size * page_count,
        'at': time.time(),
    }
