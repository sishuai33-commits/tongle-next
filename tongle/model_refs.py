"""Short, typed aliases for identity references sent to the model.

Some models frequently mis-transcribe 32-character hex ids (card ids,
source ids, receipt ids). A single wrong character invalidates a correct
human_check because the id no longer matches any known card. We therefore
replace *only* identity references -- values under a small set of identity
keys that are exactly 32 lowercase hex characters -- with short aliases in
the payload sent to the model, and map the model's structured response back
to the real ids.

Aliases are typed so the model can tell the roles apart instead of seeing
identical ``refNNN`` strings:

* ``srcNNN``     -- ``source_id`` / ``output_source`` values
* ``receiptNNN`` -- ``receipt_id`` values
* ``cardNNN``    -- ``card_id`` and generic ``id`` values not otherwise
  classified as source/receipt
* ``refNNN``     -- a real id that appears in more than one role (for example
  both a source and a card); the generic form avoids guessing a type

The same real id always reuses the same alias across the whole payload, so a
value that is known to be a source cannot be recast as a card merely because
it appears inside ``needs_cards``. Type classification is decided by a full
pre-scan of the payload, never by the request key.

Nothing else is touched: card bodies, source quotes, hashes, versions and
any string under any other key keep their exact bytes. The databases, card
bodies and source text are never rewritten by this module.

This is a pure transport transformation, not an authorization decision. A
response alias that is unknown, or a real id that was never aliased, is left
exactly as the model returned it so the existing gates reject it normally.
There is no fuzzy matching and no "repair" of near-miss ids.
"""
import re

# Values are aliased only under these keys. The keys themselves are never
# renamed, and a value under any other key is never inspected.
SOURCE_KEYS = frozenset(('source_id', 'output_source'))
RECEIPT_KEYS = frozenset(('receipt_id',))
CARD_KEYS = frozenset(('card_id', 'id'))
IDENTITY_KEYS = SOURCE_KEYS | RECEIPT_KEYS | CARD_KEYS

# A few structured fields carry a *list of bare identity strings* rather than
# an id under a named property. ``needs_cards`` is the card-read request list:
# each element is a verbatim catalog id, so it needs the same alias transport
# as a normal identity value, just applied to list elements. Only this exact
# key is affected; quote/body/text and every other key keep their bytes. The
# request key never decides the alias type: an element that is known to be a
# source elsewhere in the payload stays a source alias.
IDENTITY_LIST_KEYS = frozenset(('needs_cards',))

# Exactly 32 lowercase hexadecimal characters. Upper case, shorter, longer or
# non-hex values are business content, not identity, and are left untouched.
_HEX32 = re.compile(r'^[0-9a-f]{32}$')

# Aliases are short and fixed-width so they cannot be confused with real ids.
_ALIAS = re.compile(r'^(?:src|receipt|card|ref)[0-9]{3,}$')

_ROLE_PREFIX = {'source': 'src', 'receipt': 'receipt', 'card': 'card'}


def _alias(kind, index):
    return '%s%03d' % (kind, index)


def _kind_for(roles):
    """Pick an alias prefix from the roles a real id was seen in.

    A single role maps to its typed prefix. Zero roles (only seen as a bare
    request value) or multiple conflicting roles map to the generic ``ref``
    prefix rather than guessing one type.
    """
    if len(roles) == 1:
        return _ROLE_PREFIX[next(iter(roles))]
    return 'ref'


def _scan(node, roles, occupied, key=None):
    """Collect role assignments and reserve literal strings for collision."""
    if isinstance(node, dict):
        for k, v in node.items():
            _scan(v, roles, occupied, k)
    elif isinstance(node, list):
        for item in node:
            _scan(item, roles, occupied, key)
    elif isinstance(node, str):
        if key in IDENTITY_KEYS:
            # Reserve any literal string under an identity key, even a
            # non-hex one, so a generated alias can never shadow it.
            occupied.add(node)
            if _HEX32.fullmatch(node):
                if key in SOURCE_KEYS:
                    roles.setdefault(node, set()).add('source')
                elif key in RECEIPT_KEYS:
                    roles.setdefault(node, set()).add('receipt')
                elif key in CARD_KEYS:
                    roles.setdefault(node, set()).add('card')
        elif key in IDENTITY_LIST_KEYS:
            occupied.add(node)


def encode_references(payload):
    """Return ``(encoded, reverse_map)`` without modifying ``payload``.

    ``reverse_map`` maps each alias back to the real id. The same real id
    always reuses the same alias across the whole payload. Only identity
    values that are exactly 32 lowercase hex are aliased; lists are traversed
    recursively and every other value is copied verbatim.
    """
    roles = {}
    occupied = set()
    _scan(payload, roles, occupied)
    forward = {}
    reverse = {}
    counters = {'src': 1, 'receipt': 1, 'card': 1, 'ref': 1}

    def alias_for(value):
        alias = forward.get(value)
        if alias is not None:
            return alias
        kind = _kind_for(roles.get(value) or ())
        index = counters[kind]
        alias = _alias(kind, index)
        while alias in occupied:
            index += 1
            alias = _alias(kind, index)
        counters[kind] = index + 1
        forward[value] = alias
        reverse[alias] = value
        return alias

    def value_of(key, value):
        if (key in IDENTITY_KEYS and isinstance(value, str)
                and _HEX32.fullmatch(value)):
            return alias_for(value)
        return value

    def walk(node, key=None):
        if isinstance(node, dict):
            return {k: walk(v, k) for k, v in node.items()}
        if isinstance(node, list):
            if key in IDENTITY_LIST_KEYS:
                return [value_of('id', item) if isinstance(item, str) else walk(item, key)
                        for item in node]
            return [walk(item, key) for item in node]
        return value_of(key, node)

    encoded = walk(payload)
    return encoded, reverse


def decode_references(response, reverse_map):
    """Map aliases in ``response`` back to real ids without guessing.

    Only a value equal to a known alias, appearing under the same identity
    keys used for encoding, is replaced. Anything else -- unknown aliases,
    real ids, values under other keys, or non-string values -- is copied
    verbatim so the existing gates can reject it. No fuzzy or case-insensitive
    matching is performed.
    """
    if not isinstance(reverse_map, dict):
        return response

    def map_value(key, value):
        if key in IDENTITY_KEYS and isinstance(value, str) and value in reverse_map:
            return reverse_map[value]
        return value

    def walk(node, key=None):
        if isinstance(node, dict):
            return {k: walk(v, k) for k, v in node.items()}
        if isinstance(node, list):
            if key in IDENTITY_LIST_KEYS:
                return [reverse_map.get(item, item) if isinstance(item, str) else walk(item, key)
                        for item in node]
            return [walk(item, key) for item in node]
        return map_value(key, node)

    return walk(response)
