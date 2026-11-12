"""Concrete-subject admission for automatic memory, with source-only queries.

Generic requests need a same-session topic. A storage quantity such as 18MB
remains a condition, not an unknown project identifier. Explicit search stays
broad; admitted cards keep their complete original content.
"""
import re

from .context_retrieval import (
    concrete_cjk_subject, interpret_request, is_acknowledgement,
    resolve_query_subject, select_consumer_topic, _select_explicit_subject,
    _frame_residual, normalise_query, _has_new_topic_marker, CONSUMER_GENERIC,
    MIN_SUBJECT_PHRASE,
)

# Closed standard storage-unit set, not arbitrary digit-bearing identifiers.
# ASCII boundaries allow Chinese-adjacent values but protect GPT4, x18MB,
# 18MBps and scientific/identifier fragments. A space preserves word boundaries.
_STORAGE_QUANTITY = re.compile(
    r'(?<![A-Za-z0-9_.])\d+(?:\.\d+)?[ \t]*'
    r'(?:KiB|MiB|GiB|TiB|KB|MB|GB|TB)(?![A-Za-z0-9_])', re.IGNORECASE,
)

# Request verbs are syntax, not part of the noun following them. Keep the
# removal anchored: a quoted instruction or a verb inside a name is untouched.
_READ_PREFIX = re.compile(
    r'^(?:(?:请|麻烦|帮我|帮忙|看一下|看下|看看|读一下|读下|读读|'
    r'查一下|查下|查查|了解一下)\s*)+')
_GENERIC_OBJECTS = frozenset(('文章', '资料', '笔记', '新闻', '内容', '记录'))
_QUARTER_REPORT = re.compile(r'第([一二三四1-4])季度(?:报告|财报)')


def _query_view(text):
    """Conventional spelling only; never change captured text or note bodies.

    Ordinal quarterly-report names and their standard short form denote the
    same document type. This bounded normalization is not a semantic synonym
    engine and does not equate an annual report with a quarterly report.
    """
    return _QUARTER_REPORT.sub(
        lambda m: m[1].translate(str.maketrans('1234', '一二三四')) + '季报', text)


def _raw_subject(query):
    """A contiguous query noun may name material before any note exists.

    Reuse the existing query-frame vocabulary, not a corpus-derived list of
    banned phrases. Removing a request frame must not join unrelated words
    into an invented subject. A two-character noun such as 封签 is sufficient
    when it occupies the subject slot of a question.
    """
    from .memory_relevance import _leading_subject, query_terms
    query = _READ_PREFIX.sub('', query.strip())
    conditional = re.match(
        r'^([\u3400-\u9fff]{2,24}?)(?:数据|资料|记录|证据)?'
        r'(?:还没|尚未|未|不齐|不足|缺失|缺少|没有|不完整)'
        r'[^，。！？!?]{0,20}?(?:的时候|时)', query)
    if conditional:
        noun = conditional[1]
        if noun not in _GENERIC_OBJECTS and query_terms(noun):
            return noun
    leading = _leading_subject(query)
    lead_name = _frame_residual(leading)
    if (len(lead_name) >= 2 and lead_name not in _GENERIC_OBJECTS
            and leading.startswith(lead_name) and query_terms(lead_name)
            and leading[len(lead_name):] in CONSUMER_GENERIC | {''}):
        return lead_name
    # Conditional questions may contain frame characters inside real words
    # ("缺少", "编号错误时"). Keep the unchanged leading noun run rather
    # than requiring the entire condition to survive the frame stripper.
    prefix = 0
    while prefix < min(len(leading), len(lead_name)) and leading[prefix] == lead_name[prefix]:
        prefix += 1
    # A frame character inside a real noun is not a noun boundary. For
    # example stripping 间 from 空间 must not manufacture a subject 元图空.
    if (prefix >= MIN_SUBJECT_PHRASE and query_terms(leading[:prefix])
            and re.match(r'^(?:数据|资料|记录|证据|还没|尚未|未|不齐|不足|缺失|缺少|没有|不完整|时|的|$)',
                         leading[prefix:])):
        return leading[:prefix]
    residue = _frame_residual(normalise_query(query))
    runs = set(re.findall(r'[\u3400-\u9fff]+', query))
    # A separate first noun clause remains a subject when later clauses add
    # conditions/questions ("云杉包归档，缺签时怎么办"). Do not promote a
    # trailing action clause after a generic opening into a new subject.
    first = re.match(r'^\s*([\u3400-\u9fff]{3,32})\s*[，,:：]', query)
    if first and _frame_residual(first[1]) == first[1] and query_terms(first[1]):
        return first[1]
    if (3 <= len(residue) <= 32 and residue not in _GENERIC_OBJECTS and query_terms(residue) and any(
            run.startswith(residue) and re.fullmatch(r'[了吗呢吧啊]*', run[len(residue):])
            for run in runs)):
        return residue
    # Repeated names in a long query are still the same explicit subject.
    if len(runs) == 1:
        name = next(iter(runs))
        if (2 <= len(name) <= 32 and name not in _GENERIC_OBJECTS
                and _frame_residual(name) == name and query_terms(name)):
            return name
    wanted = query_terms(query)
    if len(wanted) == 1:
        name = next(iter(wanted))
        if (name not in _GENERIC_OBJECTS and name in runs
                and re.fullmatch(r'[\u3400-\u9fff]{2,32}', name)):
            return name
    return ''


def resolve_memory_query(cards, query, project, prior_provider=None):
    """Return the current concrete subject query, a same-session anchor, or ''.

    A project match, generic heading bigram or budget vacancy is not admission.
    Explicit unknown subjects remain current queries for source-only evidence;
    they must never borrow an older known topic.
    """
    from .memory_relevance import query_terms, named_query_terms, execution_steering
    interp = interpret_request(_query_view(query))
    if interp.ack_only or interp.no_history or is_acknowledgement(query) or execution_steering(query):
        return ''

    def concrete(text):
        return bool(query_terms(text, project)) and (
            _select_explicit_subject(cards, text, []) is not None
            or bool(named_query_terms(text, project)) or bool(_raw_subject(text)))

    if concrete(interp.text):
        return interp.text
    if prior_provider is None or _has_new_topic_marker(normalise_query(interp.text)):
        return ''
    for text, _created, _sid in reversed(list(prior_provider() or ())):
        prior = interpret_request(_query_view(text))
        if prior.no_history:
            return ''
        if not prior.ack_only and not is_acknowledgement(text) and concrete(prior.text):
            return prior.text
    return ''


def select_memory_topic(all_cards, query, project, prior_provider=None):
    """Require a current or recovered topic; preserve complete matching cards.

    Topic resolution and quantity removal are only retrieval views. Callers
    retain the actual turn for event identity and the answer.
    No cross-project candidate can be added by this fallback. Existing normal
    selection, including its explicitly authorized shared cards, is unchanged.
    """
    cards = list(all_cards or ())
    from .memory_relevance import filter_notes, named_query_terms
    query = resolve_memory_query(cards, query, project, prior_provider)
    if not query:
        return []
    selected = select_consumer_topic(cards, query, project)
    if selected:
        if not named_query_terms(query, project):
            noun = _raw_subject(query)
            if noun and not re.search(r'和|与|及|或|、', noun):
                # An explicit leading noun keeps its qualifiers even when an
                # authorized sibling shares a long suffix. An unknown sibling
                # cannot inherit the known one's rules (紫晶石 != 蓝晶石).
                selected = [card for card in selected if noun in ' '.join(
                    str(card.get(key) or '') for key in ('title', 'key', 'topic', 'body'))]
            # The legacy selector expands a pre-的 subject by bigram. In the
            # memory path keep its full CJK identity: 晶石 cannot substitute
            # for 蓝晶石, and a second explicitly named subject is preserved.
            _terms, focused, phrases = concrete_cjk_subject(query, cards)
            if focused:
                ids = {card.get('id') for card in focused}
                # A separately listed two-character name is stronger evidence
                # than a two-character overlap inside a longer name. Keep its
                # heading cards, without promoting that suffix to a phrase
                # which could pull in every sibling (蓝晶石、晶石 != 红晶石).
                lead = query.split('的', 1)[0]
                parts = re.split(r'和|与|及|或|、', lead)
                if len(parts) > 1:
                    short_names = {part.strip() for part in parts
                                   if re.fullmatch(r'[\u3400-\u9fff]{2}', part.strip())
                                   and _raw_subject(part.strip()) == part.strip()}
                    ids.update(card.get('id') for card in selected if any(
                        str(card.get(key) or '').startswith(name)
                        for name in short_names for key in ('title', 'key', 'topic')))
                selected = [card for card in selected if card.get('id') in ids or any(
                    phrase in ' '.join(str(card.get(key) or '') for key in
                                       ('title', 'key', 'topic', 'body'))
                    for phrase in phrases)]
        if _select_explicit_subject(cards, query, []) is None:
            subject = _raw_subject(query)
            selected = [card for card in selected if subject and subject in ' '.join(
                str(card.get(key) or '') for key in ('title', 'key', 'topic', 'body'))]
        return filter_notes(selected, query, project)
    interp = interpret_request(query)
    if interp.ack_only or interp.no_history or is_acknowledgement(query):
        return []
    lexical = _STORAGE_QUANTITY.sub(' ', interp.text)
    if lexical == interp.text:
        return []
    own = [card for card in cards if isinstance(card, dict)
           and card.get('project') == project]
    # Establish the entity from the original leading structure, before the
    # unknown quantity. Removing a value must not promote a generic trailing
    # action ("18MB材料怎么自动归档") into a new entity.
    _terms, subjects, _unknown = resolve_query_subject(interp.text, own)
    _cjk, cjk_subjects, _phrases = concrete_cjk_subject(
        interp.text, own, lead_only=True)
    if not subjects and not cjk_subjects:
        return []
    # Reuse the complete selector rather than accepting body-term overlap or
    # bypassing its remaining unknown-identifier / no-history checks. There is
    # a concrete current subject, so no second session-anchor lookup is needed.
    return filter_notes(select_consumer_topic(own, lexical, project),lexical,project)
