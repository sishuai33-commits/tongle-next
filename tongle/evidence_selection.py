"""Bounded, on-demand evidence reading for maintenance compilation.

This module replaces the old "all project card bodies must fit in one prompt or
nothing is published" deadlock. The design is deliberately small:

* The host keeps the complete authorized universe; the model receives a
  bounded candidate *hint* (own project cards plus read-only shared cards,
  capped by row count and bytes). The candidate scope truthfully reports that
  it is only a hint, never a claim of global completeness.
* Only a small ranked subset of card *bodies* is supplied up front. The model
  may ask for more by id (``needs_cards``) or run a restricted lexical
  ``needs_search``; the host then reads the latest complete body for those
  ids, bounded by a total character budget. A loaded body is not "read" until
  it is actually dispatched.
* Nothing here trusts model self-reporting. The host tracks exactly which
  id/version/body hash it supplied per stage, and hosts the deterministic
  duplicate checks itself.

No shell, tools, or network. This is pure data shaping over the local store.
"""
import hashlib
import json
import re

# Fixed, non-growing bounds. These are defaults; the caller may lower them but
# never derive them from the current corpus size.
DEFAULT_BODY_BUDGET_CHARS = 52000
DEFAULT_INITIAL_BODY_CHARS = 26000
DEFAULT_CATALOG_CHARS = 100000
DEFAULT_MAX_INPUT_BYTES = 300000
MAX_STAGE_CALLS = 3

# Fixed bounds for the bounded candidate scope and the restricted needs_search
# channel. The candidate scope is a *hint*, never a claim of global coverage.
DEFAULT_CANDIDATE_ROWS = 80
SEARCH_PAGE_SIZE = 12
MAX_SEARCH_QUERY_CHARS = 240
MAX_SEARCH_CURSOR = 200000
MAX_SEARCH_RECEIPTS = 12
# Bounded lexical recall of suspected semantic duplicates for a proposed create.
MAX_DUPLICATE_RELATED = 8
# Bounded, fixed ceiling on the initial-retrieval related-hit receipt. It is a
# *set* of (id, version) of cards sharing at least one token with the batch, so
# adding an unrelated card (no shared token) never changes it, and it never
# depends on corpus IDF, a relevance score or a whole-catalog hash.
MAX_RELATED_HITS = 200

# Small and stable Chinese/ASCII tokenizer shared by local recall and search.
# It never touches the corpus size, so its output is deterministic for a given
# text regardless of how many other cards exist.
_STOPWORDS = {'当前', '如何', '什么', '问题', '继续', '可以', '应该', '是否', '需要',
              '我们', '这个', '已经', '进行', '相关', '不能', '怎么', '会话', '项目'}


def terms(text):
    if not isinstance(text, str):
        return set()
    found = set(re.findall(r'[a-z0-9_]{2,}', text.lower()))
    for run in re.findall(r'[\u3400-\u9fff]+', text):
        found.update(run[i:i+2] for i in range(len(run)-1))
    return found - _STOPWORDS


def normalise_body(body):
    return ' '.join((body or '').split()).casefold()


def validate_search_request(raw):
    """Validate one ``needs_search`` value. Returns ``(info, error_label)``.

    The schema is exactly ``{"query": <bounded string>, "cursor": <int>}``;
    ``cursor`` may be omitted. Unknown fields (for example ``project`` or
    ``scope``), a non-object, a missing/blank/overlong query, control
    characters and out-of-range cursors are rejected with a short stable
    label. The query is always *data* passed to the existing authorized lexical
    search: it can never execute a command, SQL statement or scope change.
    """
    if not isinstance(raw, dict):
        return None, 'search_not_object'
    if set(raw) - {'query', 'cursor'}:
        return None, 'search_unknown_field'
    query = raw.get('query')
    if not isinstance(query, str) or not query.strip():
        return None, 'search_query_invalid'
    if len(query) > MAX_SEARCH_QUERY_CHARS:
        return None, 'search_query_too_long'
    if any(ord(c) < 32 or ord(c) == 127 for c in query):
        return None, 'search_query_control_chars'
    cursor = raw.get('cursor', 0)
    if isinstance(cursor, bool) or not isinstance(cursor, int):
        return None, 'search_cursor_invalid'
    if cursor < 0 or cursor > MAX_SEARCH_CURSOR:
        return None, 'search_cursor_range'
    return {'query': query, 'cursor': cursor}, None


def search_page(cards, query, cursor=0, page_size=SEARCH_PAGE_SIZE):
    """Deterministic IDF-free lexical page over ``cards``.

    Each card is scored only against the query (title/topic/key weighted above
    body); the score never depends on how many other cards exist, so adding an
    unrelated card cannot change the result set or the page order. Ties break
    on the card id. Returns ``(page, total)`` where ``page`` is a list of
    ``(score, card)`` and ``total`` is the number of matching cards.
    """
    q = terms(query)
    if not q:
        return [], 0
    scored = []
    for card in cards:
        heading = ' '.join(str(card.get(k) or '') for k in ('title', 'topic', 'key')).lower()
        full = heading + ' ' + str(card.get('body') or '').lower()
        score = 0
        for term in q:
            if term in heading:
                score += 2
            elif term in full:
                score += 1
        if score > 0:
            scored.append((-score, str(card.get('id') or ''), card))
    scored.sort(key=lambda item: (item[0], item[1]))
    total = len(scored)
    page = [(entry[0], entry[2]) for entry in scored[cursor:cursor + page_size]]
    return page, total


def _recall_rows(cards):
    """Precompute one bounded token set per authorized card.

    Stable and independent of corpus size, so the deterministic duplicate
    recall never depends on how many unrelated cards exist.
    """
    rows = []
    for card in cards:
        if not isinstance(card, dict) or not isinstance(card.get('id'), str):
            continue
        text = ' '.join(str(card.get(k) or '') for k in ('key', 'title', 'topic', 'body'))
        rows.append((card, terms(text)))
    return rows


def _create_recall(card_rows, op):
    """Return ``(exact, related)`` for one proposed create.

    ``exact`` is a list of ``(id, version)`` identity collisions (key, title or
    normalized full body). ``related`` is the bounded list (<= ``MAX_DUPLICATE_RELATED``)
    of lexical suspects, exact collisions already removed, ordered by
    descending overlap then id. Shared by :func:`duplicate_report` (which
    collapses to global sets) and :func:`related_pairs` (which keeps the
    per-operation identity the paged comparison needs).
    """
    key = op.get('key') if isinstance(op, dict) else None
    title = op.get('title') if isinstance(op, dict) else None
    body = op.get('body') if isinstance(op, dict) else None
    norm = normalise_body(body) if isinstance(body, str) and body else ''
    wanted_terms = terms(' '.join(str(op.get(k) or '') for k in ('key', 'title', 'body')))
    exact = []
    rel = []
    for card, card_terms in card_rows:
        if ((isinstance(key, str) and key and card.get('key') == key)
                or (isinstance(title, str) and title and card.get('title') == title)
                or (norm and normalise_body(card.get('body')) == norm)):
            exact.append((card['id'], card.get('version')))
            continue
        if wanted_terms:
            overlap = len(wanted_terms & card_terms)
            if overlap:
                rel.append((overlap, card['id'], card.get('version')))
    rel.sort(key=lambda item: (-item[0], item[1]))
    return exact, [(entry[1], entry[2]) for entry in rel[:MAX_DUPLICATE_RELATED]]


def duplicate_report(cards, operations):
    """Host-side deterministic duplicate signal for proposed creates.

    ``cards`` is the full authorized universe (including retired and shared
    cards). Returns ``(exact, related, batch_conflicts)``:

    * ``exact`` -- ``(id, version)`` of existing cards whose key, title or
      normalized body collides deterministically with a proposed create. The
      model may never release an exact duplicate.
    * ``related`` -- ``(id, version)`` of bounded lexical candidates (suspected
      semantic duplicates, including retired cards) that must be read in full
      and re-judged before a create is accepted. The comparison uses the
      proposed key/title **and body** against every card's key/title/topic
      **and body**, not only the heading: a retired card whose body is the
      same claim under a different title is still recalled. The union is
      bounded by ``MAX_DUPLICATE_RELATED`` per create, so it can reach
      ``8 * ops`` entries; it is deliberately *not* cut down to a smaller
      fixed subset.
    * ``batch_conflicts`` -- short labels for collisions *within the same
      batch* (for example two creates sharing a key), which the host rejects
      without consulting the model.
    """
    creates = [op for op in (operations or [])
               if isinstance(op, dict) and op.get('op') == 'create']
    if not creates:
        return [], [], []
    exact = set()
    related = set()
    batch = []
    seen_keys = set()
    seen_titles = set()
    seen_bodies = set()
    for op in creates:
        key = op.get('key')
        title = op.get('title')
        body = op.get('body')
        norm = normalise_body(body) if isinstance(body, str) and body else ''
        if isinstance(key, str) and key:
            if key in seen_keys:
                batch.append('batch_key:' + key[:60])
            seen_keys.add(key)
        if isinstance(title, str) and title:
            if title in seen_titles:
                batch.append('batch_title:' + title[:60])
            seen_titles.add(title)
        if norm:
            if norm in seen_bodies:
                batch.append('batch_body')
            seen_bodies.add(norm)
    # Precompute each existing card's exact identity fields and full-body term
    # set once, so body recall is stable and never re-tokenized per create.
    card_rows = _recall_rows(cards)
    for op in creates:
        ex, rel = _create_recall(card_rows, op)
        exact.update(ex)
        related.update(rel)
    related -= exact
    return sorted(exact), sorted(related), sorted(set(batch))


def related_pairs(cards, operations):
    """Per-operation bounded related pairs ``(op index, card id, version)``.

    Unlike :func:`duplicate_report`, which collapses the suspected semantic
    duplicates to a global set, this preserves *which* create matched each
    card, so a downstream independent comparison can cover every
    (operation, card) pair and never claims the pairs are a Cartesian product.
    Exact identity collisions are excluded (they are host-blocked separately),
    matching ``related -= exact`` in :func:`duplicate_report`. The union is
    bounded by ``MAX_DUPLICATE_RELATED`` per create, i.e. at most ``8 * ops``.
    """
    card_rows = _recall_rows(cards)
    exact_ids = set()
    pairs = []
    for index, op in enumerate(operations or []):
        if not isinstance(op, dict) or op.get('op') != 'create':
            continue
        ex, rel = _create_recall(card_rows, op)
        for cid, _ver in ex:
            exact_ids.add(cid)
        for cid, ver in rel:
            pairs.append({'op': index, 'card_id': cid, 'version': ver})
    return [p for p in pairs if p['card_id'] not in exact_ids]


def lexical_hits(cards, query, limit=MAX_RELATED_HITS):
    """Deterministic, corpus-size-independent related-hit receipt.

    Returns an id-sorted list of ``[id, version]`` for every card sharing at
    least one token with ``query`` (key/title/topic/body), capped at a fixed
    ``limit`` after ordering by overlap then id. It never uses corpus IDF, a
    relevance score, or a whole-catalog hash, so adding a completely unrelated
    card cannot change it. Replaying this set before publish blocks an old
    approval when a *relevant* card appears or moves during processing, even
    for an update/no-op batch with no proposed create.
    """
    limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else MAX_RELATED_HITS
    q = terms(query)
    if not q:
        return []
    hits = []
    for card in cards:
        if not isinstance(card, dict) or not isinstance(card.get('id'), str):
            continue
        text = ' '.join(str(card.get(k) or '') for k in ('key', 'title', 'topic', 'body'))
        overlap = len(q & terms(text))
        if overlap:
            hits.append((overlap, card['id'], card.get('version')))
    hits.sort(key=lambda item: (-item[0], item[1]))
    selected = hits[:limit]
    return sorted([[cid, ver] for _overlap, cid, ver in selected])


def body_sha256(body):
    return hashlib.sha256((body or '').encode()).hexdigest()


def _nonneg_int(value, default):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


def _clamp(value, low, high, default):
    value = _nonneg_int(value, default)
    return max(low, min(high, value))


class Selection:
    """Result of one bounded body selection over an authorized card set.

    ``catalog_complete`` is retained for compatibility but is no longer a
    coverage claim: the model now receives a bounded candidate *hint* over the
    host's complete authorized universe, and the host -- not the model -- holds
    the full universe for authorization, duplicate checking and reads.
    """

    def __init__(self, catalog, cards, catalog_complete=True,
                 unresolved_required=None):
        self.catalog = catalog
        self.cards = cards
        self.catalog_complete = catalog_complete
        self.unresolved_required = unresolved_required or []

    def identifier(self):
        payload = {
            'catalog': [(c['id'], c.get('version')) for c in self.catalog],
            'cards': [(c['id'], c.get('version')) for c in self.cards],
            'catalog_complete': self.catalog_complete,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def catalog_entries(cards, limit=None):
    """Short, bounded catalog rows: identity + title + host-computed length.

    Body hashes are deliberately omitted: they are only needed for the host's
    own read-set receipts, which the host holds out-of-band. Keeping them out
    of the serialized catalog lets several hundred production cards fit the
    fixed catalog byte bound. ``body_chars`` is the host-computed length of the
    current body only, so the model can size its ``needs_cards`` requests
    against the advertised ``read_budget`` without receiving any body text.
    """
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        cards = cards[:limit]
    rows = []
    for card in cards:
        rows.append({
            'id': card['id'],
            'project': card.get('project'),
            'key': card.get('key'),
            'title': card.get('title'),
            'topic': card.get('topic'),
            'kind': card.get('kind'),
            'state': card.get('state'),
            'version': card.get('version'),
            'body_chars': len(card.get('body') or ''),
            'manually_edited': bool(card.get('manually_edited')),
        })
    return rows


def catalog_is_bounded(catalog, char_limit=None):
    char_limit = _clamp(char_limit, 1000, 400000, DEFAULT_CATALOG_CHARS)
    return len(json.dumps(catalog, ensure_ascii=False).encode('utf-8')) <= char_limit


def candidate_catalog(cards, ranked, required_ids, char_limit=None,
                      max_rows=DEFAULT_CANDIDATE_ROWS):
    """A bounded, fixed-size *hint* of candidate cards for the model.

    Mandatory/source-dependent ids are always included first (they are the
    required reading), then positively-ranked candidates. Zero-score
    (unrelated) cards are deliberately excluded: they add no reading value and
    would otherwise let a large unrelated library grow the payload. The result
    is capped by both row count and the fixed byte bound, so a very large
    authorized library never expands the model payload. ``catalog_complete``
    is no longer derived here: the model is told this is a bounded candidate
    scope and that the host holds the complete authorized universe.
    """
    char_limit = _clamp(char_limit, 1000, 400000, DEFAULT_CATALOG_CHARS)
    max_rows = max_rows if isinstance(max_rows, int) and not isinstance(max_rows, bool) and max_rows > 0 else DEFAULT_CANDIDATE_ROWS
    by_id = {c['id']: c for c in cards}
    ordered = []
    seen = set()
    # Required ids are deterministically sorted so the selection identity does
    # not depend on set iteration order. They are always included first.
    for cid in sorted(c for c in (required_ids or ()) if isinstance(c, str)):
        card = by_id.get(cid)
        if card is not None and cid not in seen:
            seen.add(cid)
            ordered.append(card)
    # Only cards with a strictly positive relevance score are candidates; a
    # score-0 (unrelated) card never takes a row, so an unrelated library
    # growth cannot expand the payload or perturb the cached selection. The
    # ranked list is descending, so the first non-positive score ends the loop.
    for score, _, card in ranked:
        if score <= 0:
            break
        if card['id'] in seen:
            continue
        seen.add(card['id'])
        ordered.append(card)
        if len(ordered) >= max_rows:
            break
    rows = catalog_entries(ordered[:max_rows])
    # Trim from the tail until the row list fits the fixed byte bound.
    while rows and not catalog_is_bounded(rows, char_limit):
        rows.pop()
    return rows


def mandatory_ids(cards, query_sources):
    """Source-dependent existing cards that must be candidates regardless of rank.

    A card that cites a source in the current batch is directly relevant even
    when lexical ranking would not surface it, because it may already encode the
    same decision. Also include cards whose basis depends on a queried card.
    """
    source_ids = set()
    for part in query_sources or []:
        sid = part.get('source_id')
        if isinstance(sid, str):
            source_ids.add(sid)
    required = set()
    by_id = {c['id']: c for c in cards}
    for card in cards:
        for ref in card.get('evidence', []) or []:
            if ref.get('source_id') in source_ids:
                required.add(card['id'])
                break
    # Pull one level of reverse basis so a direct dependency is visible too.
    for card in cards:
        for ref in card.get('basis_cards', []) or []:
            if ref.get('card_id') in required:
                required.add(card['id'])
                break
    return required


def select_bodies(cards, ranked, query, char_budget=None, mandatory=None,
                  body_budget_chars=None):
    """Choose full-card bodies within ``char_budget``.

    ``ranked`` is an ordered list of (score, updated_at, card). Mandatory ids
    are attempted first (they represent source/dependency relevance), then the
    ranked order -- but only candidates with a strictly positive relevance
    score. A zero-score card stays available in the complete catalog for
    duplicate comparison and explicit ``needs_cards`` reads; it is not pushed
    into the initial body budget ahead of a more relevant one. An oversized
    card is never silently truncated: it is recorded in ``unresolved_required``
    if mandatory, or left as a catalog entry otherwise.
    """
    char_budget = _clamp(char_budget, 2000, 400000, DEFAULT_INITIAL_BODY_CHARS)
    total_budget = _clamp(body_budget_chars, char_budget, 400000, DEFAULT_BODY_BUDGET_CHARS)
    by_id = {c['id']: c for c in cards}
    mandatory = set(mandatory or ())
    chosen = []
    used = 0
    unresolved = []

    def add(card):
        nonlocal used
        body = card.get('body') or ''
        size = len(body)
        if size > total_budget:
            return False
        if used + size > char_budget and card['id'] not in mandatory:
            return False
        if used + size > total_budget:
            return False
        chosen.append(card)
        used += size
        return True

    for cid in mandatory:
        card = by_id.get(cid)
        if card is None:
            continue
        if not add(card):
            unresolved.append(cid)

    for score, _, card in ranked:
        if card['id'] in mandatory:
            continue
        if score <= 0:
            continue
        if len(chosen) >= 64:
            break
        add(card)
        if used >= char_budget:
            break
    return chosen, unresolved, used


def resolve_requested(catalog, requested, already_supplied):
    """Authorize a model ``needs_cards`` request against the catalog.

    Returns ``(accepted_card_ids, rejected)``. Only ids present in the catalog
    are accepted; foreign/unknown ids are recorded as rejected. Ids already
    supplied are ignored (not re-read). Unknown aliases (``refNNN`` that the
    host never handed out, or an unknown real id) are rejected here, never
    guessed. The returned list is bounded downstream by the body budget.
    """
    allowed = {c['id'] for c in catalog}
    supplied = set(already_supplied or ())
    accepted = []
    rejected = []
    seen = set()
    if not isinstance(requested, list):
        return accepted, [{'reason': 'not_a_list'}]
    for raw in requested:
        if not isinstance(raw, str) or not raw:
            rejected.append({'id': raw if isinstance(raw, str) else None,
                             'reason': 'invalid_id'})
            continue
        if raw in seen:
            continue
        seen.add(raw)
        if raw not in allowed:
            rejected.append({'id': raw, 'reason': 'not_in_catalog'})
            continue
        if raw in supplied:
            rejected.append({'id': raw, 'reason': 'already_supplied'})
            continue
        accepted.append(raw)
    return accepted, rejected


def basis_closure_report(cards, roots, limit=64):
    """Transitive basis-card ids plus explicit problems, cycle-safe.

    Returns ``(ids, problems)`` where ``problems`` is a list of
    ``{'id', 'reason'}``. Unlike the lenient closure below, a basis reference
    to a foreign/missing card is *reported* (``out_of_scope``) instead of
    silently disappearing through filtering, and exceeding ``limit`` is
    reported as ``overflow`` on the id that could not be expanded. Both
    retained and newly named dependencies are walked recursively, so a
    dependency introduced by an update is required exactly like the original.
    """
    limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else 64
    allowed = {c['id'] for c in cards}
    basis = {}
    for card in cards:
        deps = []
        for ref in card.get('basis_cards', []) or []:
            if isinstance(ref, dict) and isinstance(ref.get('card_id'), str):
                deps.append(ref['card_id'])
        basis[card['id']] = deps
    out = set()
    problems = []
    seen_problem = set()

    def report(cid, reason):
        key = (cid, reason)
        if cid and key not in seen_problem:
            seen_problem.add(key); problems.append({'id': cid, 'reason': reason})

    def walk(cid):
        stack = [cid]
        while stack:
            cid = stack.pop()
            if cid in out:
                continue
            if cid not in allowed:
                report(cid, 'out_of_scope'); continue
            if len(out) >= limit:
                report(cid, 'overflow'); return
            out.add(cid)
            for dep in basis.get(cid, ()):
                if dep not in out:
                    stack.append(dep)

    for root in roots:
        if not isinstance(root, str) or not root:
            continue
        walk(root)
    return out, problems
