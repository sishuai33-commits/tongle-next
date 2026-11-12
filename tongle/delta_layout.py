"""Pure layout guards; reject new damage without rewriting historical text.

Retained spans bind historical duplicate allowances to the original bytes and
positions. The low-level card_delta concatenation contract is unchanged.
"""
import re

_HEADING_RE = re.compile(r'^ {0,3}(#{1,6})[ \t]+(.*)$')
_FENCE_RE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')
_BLOCK_RE = re.compile(r'^\s*(?:#{1,6}\s|\||`{3,}|~{3,}|>|[-+*]\s|\d+[.)]\s|[-=*_]{3,}\s*$)')


def _scan(body):
    lines = body.splitlines(keepends=True)
    headings, parents = [], []
    fence_char, fence_length, offset = '', 0, 0
    for index, raw in enumerate(lines):
        line = raw.rstrip('\r\n')
        fence = _FENCE_RE.match(line)
        if fence:
            char = fence.group(1)[0]
            if not fence_char:
                if not (char == '`' and '`' in fence.group(2)):
                    fence_char, fence_length = char, len(fence.group(1))
            elif (char == fence_char and len(fence.group(1)) >= fence_length
                  and not fence.group(2).strip()):
                fence_char = ''
        elif not fence_char:
            match = _HEADING_RE.match(line)
            if match:
                title = re.sub(r'[ \t]+#+[ \t]*$', '', match.group(2)).strip()
                if title:
                    level = len(match.group(1))
                    parents = [(depth, name) for depth, name in parents if depth < level]
                    headings.append((index, offset, offset + len(line), level, title, tuple(parents)))
                    parents.append((level, title))
        offset += len(raw)
    pairs = []
    for prev, nxt in zip(headings, headings[1:]):
        if prev[3:5] == nxt[3:5] and all(not lines[k].strip() for k in range(prev[0]+1, nxt[0])):
            pairs.append((prev[1], nxt[2], prev[3:5]))
    return pairs, bool(fence_char)


def adjacent_heading_duplicates(body):
    return len(_scan(body)[0])


def _ordinary_eof(prefix):
    if not prefix or _scan(prefix)[1]:
        return False
    tail = prefix.splitlines()[-1]
    return bool(tail.strip()) and not tail.startswith(('    ', '\t')) and not _BLOCK_RE.match(tail)


def check_piece_junctions(pieces, where, allow_inline_eof=False):
    """Require explicit separation at every newly written block junction.

    Preserve ordinary one-line prose continuation at EOF, but never append
    directly to a heading, table/list marker or a fenced/indented code line.
    """
    if not isinstance(pieces, list):
        raise ValueError(f'{where} pieces must be a list')
    for index, ((left_text, left_new), (right_text, right_new)) in enumerate(zip(pieces, pieces[1:])):
        if not (left_new or right_new):
            continue
        if not isinstance(left_text, str) or not isinstance(right_text, str):
            raise ValueError(f'{where} piece text missing')
        if not left_text or not right_text or left_text[-1] in '\r\n' or right_text[0] in '\r\n':
            continue
        if (allow_inline_eof and index == len(pieces)-2 and right_new and not left_new
                and '\n' not in right_text and '\r' not in right_text
                and not right_text.startswith(('    ', '\t')) and not _BLOCK_RE.match(right_text)
                and _ordinary_eof(''.join(text for text, _ in pieces[:index+1]))):
            continue
        raise ValueError(f'{where} edit loses block separation: supply explicit newlines around the changed block')


def check_heading_duplicates(old_body, new_body, where, retained_ranges=None):
    """Only an unchanged original pair may survive as a historical duplicate.

    Each retained range is (new_start, new_end, old_start), supplied by the
    host compiler's actual concatenation. Counts cannot transfer an allowance
    from a deleted old pair to a newly written pair elsewhere.
    """
    old_body = old_body or ''
    if retained_ranges is None:
        retained_ranges = [(0, len(old_body), 0)] if old_body == new_body else []
    old_pairs = set(_scan(old_body)[0])
    for start, end, identity in _scan(new_body)[0]:
        cursor, old_start = start, None
        for lo, hi, origin in retained_ranges:
            if hi <= cursor:
                continue
            if lo > cursor:
                break
            mapped = origin + cursor - lo
            if old_start is None:
                old_start = mapped
            if mapped != old_start + cursor - start:
                break
            cursor = min(hi, end)
            if cursor == end:
                break
        if (cursor != end or old_start is None
                or (old_start, old_start + end-start, identity) not in old_pairs
                or old_body[old_start:old_start+end-start] != new_body[start:end]):
            raise ValueError(f'{where} introduces an adjacent duplicate same-level heading: edit the existing section instead')
