"""Bounded separation of a consumer turn's *discussion* from inert output syntax.

The consumer path must answer "what is this turn about?" from the discussion the
user is having, not from the output syntax they paste around it. Three bounded
mechanisms do that, all fixed and corpus-independent:

* **Declared fields.** A field/label cue (``字段``/``字段名``/``标签`` ...)
  followed by a *finite declared-field connector* (``叫``/``为``/``是``/``名为``)
  and one or more ASCII identifiers joined by ``与``/``和``/comma is a field
  declaration -- ``字段叫 canopyAction 与 latchCheck`` -- and the identifiers are
  removed. A code identifier that is *discussed* rather than declared carries no
  such cue/connector and is preserved.

* **Inert structural syntax, scoped by a cue.** A fenced block, XML/HTML tag,
  quoted/backticked span or balanced ``{...}``/``[...]`` literal is treated as an
  output example *only when an output/format cue stands in the same clause*
  before it. The cue may be an ordinary answer verb (``回复``/``作答``/``以 …
  回答``) as well as ``输出``/``返回``; a discussed ``分析日志中的 JSON 对象``
  carries no such verb and is kept. The clause boundary includes the comma:
  a cue cannot reach across ``，`` into a fresh clause that discusses the
  literal itself (``…，这个对象 {...} 合法吗``), yet still scopes a literal
  that is merely adjacent to the cue or re-attached by inert format
  metalanguage (``…，不是讨论对象：{...}``). Syntactic structure is never assumed to be irrelevant: a fenced
  ``BirchRelay()`` being explained, a JSON configuration of ``BirchRelay`` or an
  explicitly discussed ``<BirchRelay/>`` component carry no output cue and are
  kept, so the real subject survives; an explicitly requested output/schema
  literal is suppressed. A genuinely *unknown* named subject still blanks.

* **Inert format clauses.** After a literal is removed the metalanguage around
  it (``先约定输出 JSON 形状``) can still leak a generic word such as ``约定`` into
  the lexical query and match unrelated headings. A bounded clause that carries a
  format cue and reduces to nothing but format metalanguage is therefore dropped
  *as a clause*, so the genuine discussion before and after it is preserved
  rather than the whole turn being blanked.

``discussion_text`` returns the turn with those inert spans/clauses replaced by
spaces; the original text is never mutated and the transformation is idempotent
(applying it to an already-separated turn is a no-op), so the nested calls made
by the consumer path cannot delete new text on a second pass.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Bounded vocabularies. All fixed; nothing here is derived from the corpus or
# from an answer.
# ---------------------------------------------------------------------------

# Declared field / label vocabulary: an identifier adjacent to one of these is a
# field name or a label, never a discussion object.
_FIELD_CUES = (
    "字段列表", "字段名", "字段", "键名", "列名", "表头", "标签",
    "占位符", "占位", "输出字段", "返回字段",
    "field", "fields", "label", "labels", "schema", "placeholder",
)

# Explicit output-format / shape statements: the material after the cue is the
# requested output shape, never the topic.
_FORMAT_PHRASE_CUES = (
    "不是新讨论对象", "不是讨论对象", "新讨论对象", "讨论对象",
    "输出形状", "输出格式", "输出字段", "输出示例", "返回格式", "返回形状",
    "返回字段", "只是形状", "只是格式", "只是示例", "只是样例",
    "示例输出", "样例输出", "模板为", "模板是", "形状为", "形状是",
    "格式为", "格式是", "output shape", "output format",
)

# A format *verb* near a format *noun* (either order, small window) is also a
# cue: ``输出 JSON`` / ``用 XML 输出`` / ``约定输出形状``. Keeping the verb and the
# noun separate is what stops a genuine phrase such as ``这段代码的输出`` (no format
# noun) from being mistaken for an output request. Ordinary Chinese *answer*
# verbs (``回复``/``作答``/``以 … 回答``) are the same request paraphrased: the
# verb is what varies, the format noun next to it is what still scopes the
# inertness, so ``分析日志中的 JSON 对象`` (no answer verb) is never a request.
_FORMAT_VERBS = ("输出", "返回", "给出", "提供", "约定",
                 "回复", "作答", "回答", "答复",
                 "return", "output", "render")
_FORMAT_NOUNS = (
    "形状", "格式", "字段", "键名", "列名", "表头", "标签", "示例", "样例",
    "模板", "占位符", "占位", "schema", "field", "label", "placeholder",
    "json", "xml", "html", "yaml", "toml", "csv", "sql",
)

# Data-format names that are inert *inside an otherwise format-only clause*.
_FORMAT_NAMES = (
    "json", "xml", "html", "yaml", "toml", "csv", "sql", "schema",
)

# Non-CJK format metalanguage removed when deciding whether a clause is inert.
# Longest first so multi-character phrases disappear before their parts.
_FORMAT_WORDS = (
    "不是新讨论对象", "不是讨论对象", "新讨论对象", "讨论对象",
    "输出形状", "输出格式", "输出字段", "输出示例", "返回格式", "返回形状",
    "返回字段", "只是形状", "只是格式", "只是示例", "只是样例",
    "示例输出", "样例输出",
    "约定", "输出", "返回", "回复", "作答", "回答", "答复",
    "形状", "格式", "字段", "键名", "列名", "表头",
    "标签", "示例", "样例", "模板", "占位符", "占位",
    "如下", "以下", "下面", "先", "只", "仅仅", "只是", "不是", "新",
    "讨论", "对象", "请", "用", "以", "按", "照",
    "叫", "叫做", "叫作", "名为", "为", "是",
    "与", "和", "及", "以及", "跟", "同", "一个",
    "schema", "placeholder", "field", "fields", "label", "labels",
)
_FORMAT_WORDS_SORTED = tuple(sorted(set(_FORMAT_WORDS), key=len, reverse=True))

# A clause that is *only* one of these ambiguous single words is kept: it can be
# discussed content (a variable literally named ``输出``) rather than a request.
_BARE_CUE_WORDS = frozenset(("输出", "返回", "形状", "格式", "示例", "样例", "模板"))

_ASCII_TOKEN = re.compile(r'[A-Za-z0-9_]+')
_CJK_CHAR = re.compile(r'[\u3400-\u9fff]')

# ---------------------------------------------------------------------------
# Structural output syntax (fences / tags / quotes / balanced literals).
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r'```.*?(?:```|$)|~~~.*?(?:~~~|$)', re.S)
_TAG_RE = re.compile(r'</?[A-Za-z][A-Za-z0-9_.:-]*(?:\s[^<>]*?)?/?>')
_QUOTED_RE = re.compile(
    '`([^`]+)`'                  # backticked span
    '|"([^"\n]*)"'               # double-quoted span
    '|“([^”]*)”'                 # curly double-quoted span
    "|'([^'\n]*)'")              # single-quoted span

_QUOTE_OPENERS = '`"\u201c\''

# How far back a format cue is looked for before a structural literal.
_CUE_WINDOW = 48

# Structure punctuation that marks a non-CJK balanced body as data.
_DATA_PUNCT_RE = re.compile(r"""[:;"'{},()\[\],;|]""")

# Punctuation/whitespace stripped when deciding whether a clause is inert. It
# deliberately leaves backticks, braces and angle brackets in place so a
# preserved code/json literal still counts as *content*.
_PUNCT_ONLY_RE = re.compile(r'[\s，。！？；：、,.!?;:()（）"\'“”‘’~～…·]')


def _looks_like_data(inner):
    """Whether a balanced ``{...}``/``[...]`` body is a data literal.

    A CJK body is discussion text unless it carries explicit JSON punctuation
    and an ASCII key (a foreign-key style ``{"role":"巡查"}`` object); a
    non-CJK body is inert data when it carries explicit structure punctuation or
    is a bare declared identifier. This keeps a Chinese placeholder such as
    ``{角色}`` as a discussion object while removing an inline JSON schema.
    """
    stripped = inner.strip()
    if not stripped:
        return True
    if _CJK_CHAR.search(stripped):
        return bool(re.search(r'[:"]', stripped) and _ASCII_TOKEN.search(stripped))
    if _DATA_PUNCT_RE.search(stripped):
        return True
    return bool(re.fullmatch(r'[A-Za-z0-9_.-]+', stripped))


def _balanced_span(text, i, opener, closer):
    """End index (exclusive) of the balanced literal starting at ``i``, or None."""
    depth, j, n = 0, i, len(text)
    while j < n:
        if text[j] == opener:
            depth += 1
        elif text[j] == closer:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return None


def _has_format_cue(prefix):
    """Whether a bounded text span states/labels an output format.

    True for an explicit format phrase, or for a format *verb* close to a format
    *noun* (either order). A mere ``输出``/``返回`` inside ordinary prose such as
    ``这段代码的输出`` is not enough.
    """
    low = (prefix or '').lower()
    if any(phrase in low for phrase in _FORMAT_PHRASE_CUES):
        return True
    for verb in _FORMAT_VERBS:
        start = 0
        while True:
            idx = low.find(verb, start)
            if idx < 0:
                break
            window = low[max(0, idx - 8): idx + len(verb) + 8]
            if any(noun in window for noun in _FORMAT_NOUNS):
                return True
            start = idx + len(verb)
    return False


def _cue_before(text, pos):
    """Whether a format cue stands in the same clause before ``pos``.

    The lookup never crosses a sentence terminator, so an output cue in an
    earlier sentence cannot make a *later* discussed code literal look inert.
    A newline is *not* a cutoff: a labelled fenced schema puts its format label
    on an earlier line, and that cue must still scope the fence.

    A comma also closes the cue's clause: a cue cannot reach across a comma
    into a fresh clause that *discusses* the literal itself (``…，这个对象
    {...} 合法吗``). The cue still scopes the literal when whatever follows
    the comma is itself inert -- nothing at all (``输出 JSON，{...}``) or
    mere format metalanguage (``…输出形状，不是讨论对象：{...}``), so a
    labelled schema after a comma is still removed.
    """
    segment = text[max(0, pos - _CUE_WINDOW):pos]
    for sep in ('。', '！', '？', '；', '!', '?', ';'):
        cut = segment.rfind(sep)
        if cut >= 0:
            segment = segment[cut + 1:]
    cut = max(segment.rfind('，'), segment.rfind(','))
    if cut >= 0:
        tail = segment[cut + 1:]
        if tail.strip() and not _format_only(tail):
            segment = tail
    return _has_format_cue(segment)


def _structural_span(text, i):
    """The structural literal starting at ``i`` (a string), or ``None``."""
    ch = text[i]
    if text.startswith('```', i) or text.startswith('~~~', i):
        match = _FENCE_RE.match(text, i)
        return match.group(0) if match else None
    if ch == '<':
        match = _TAG_RE.match(text, i)
        return match.group(0) if match else None
    if ch in '{[':
        closer = '}' if ch == '{' else ']'
        end = _balanced_span(text, i, ch, closer)
        if end is not None and _looks_like_data(text[i + 1:end - 1]):
            return text[i:end]
        return None
    if ch in _QUOTE_OPENERS:
        match = _QUOTED_RE.match(text, i)
        return match.group(0) if match else None
    return None


def _strip_inert_structure(text):
    """Remove structural literals that sit under an output/format cue.

    A literal with *no* cue in its clause is kept verbatim (it may be the
    discussed subject), so this never unconditionally erases code/data.
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        span = _structural_span(text, i)
        if span is not None:
            if _cue_before(text, i):
                out.append(' ')
            else:
                out.append(span)
            i += len(span)
            continue
        out.append(text[i])
        i += 1
    return ''.join(out)


# ---------------------------------------------------------------------------
# Declared field runs (cue + connector + identifiers).
# ---------------------------------------------------------------------------

_TOKEN = r'[A-Za-z0-9_]+'
# Identifiers joined by a list separator or by a CJK coordinating connector.
_RUN_SEP = (r'(?:[\s,;、/|，]*(?:与|和|及|以及|跟|同)[\s,;、/|，]*'
            r'|[\s,;、/|，]+)')
_RUN = _TOKEN + r'(?:' + _RUN_SEP + _TOKEN + r'){0,7}'
_RUN_ALT = '|'.join(re.escape(cue)
                    for cue in sorted(set(_FIELD_CUES), key=len, reverse=True))
_FORMAT_RUN_RE = re.compile(
    r'(?P<cue>' + _RUN_ALT + r')'
    r'[\s:：=]*(?:叫|叫做|叫作|名为|是|为)?[\s:：=]*'
    r'(?P<run>' + _RUN + r')',
    re.IGNORECASE)


def _strip_format_runs(text):
    """Remove ASCII identifiers declared as fields by a field/label cue."""
    if not any(cue in text.lower() for cue in _FIELD_CUES):
        return text
    return _FORMAT_RUN_RE.sub(lambda m: m.group('cue'), text)


# ---------------------------------------------------------------------------
# Inert format clauses.
# ---------------------------------------------------------------------------

_SENT_TOKEN_RE = re.compile(r'([。！？；\n])')
_CLAUSE_TOKEN_RE = re.compile(r'([，,；;。！？\n])')


def _format_only(clause):
    """Whether a clause is nothing but format metalanguage / a literal.

    Returns False for an empty clause or one carrying any concrete CJK content
    or arbitrary ASCII identifier, so genuine discussion is never reduced to a
    format clause.
    """
    if not clause.strip():
        return False
    if _PUNCT_ONLY_RE.sub('', clause) in _BARE_CUE_WORDS:
        return False
    value = clause
    for word in _FORMAT_WORDS_SORTED:
        value = value.replace(word, '')
    low = value.lower()
    for name in _FORMAT_NAMES:
        low = low.replace(name, '')
    low = _ASCII_TOKEN.sub('#', low)       # any other ASCII token is content
    low = _PUNCT_ONLY_RE.sub('', low)
    return not low


def _drop_format_clauses_in(part):
    pieces = _CLAUSE_TOKEN_RE.split(part)
    for idx in range(0, len(pieces), 2):
        if _format_only(pieces[idx]):
            pieces[idx] = ' '
    return ''.join(pieces)


def _strip_format_clauses(text):
    """Drop bounded clauses that are nothing but format metalanguage."""
    if not _has_format_cue(text):
        return text
    out = []
    for idx, part in enumerate(_SENT_TOKEN_RE.split(text)):
        if idx % 2 == 1:
            out.append(part)               # sentence delimiter
        elif _has_format_cue(part):
            out.append(_drop_format_clauses_in(part))
        else:
            out.append(part)
    return ''.join(out)


def discussion_text(text):
    """The requested discussion of ``text`` with inert output syntax removed.

    Handles declared field lists (with finite connectors), inline JSON/XML, a
    labelled fenced schema, and format metalanguage before or after the actual
    discussion -- while preserving a code/data literal that *is* the subject.
    Returns a new string; the input is never mutated. An empty / non-string
    input is ``''``. The transform is idempotent.
    """
    if not isinstance(text, str) or not text:
        return ''
    cleaned = _strip_inert_structure(text)
    cleaned = _strip_format_runs(cleaned)
    cleaned = _strip_format_clauses(cleaned)
    return cleaned
