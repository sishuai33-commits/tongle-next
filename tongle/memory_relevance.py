"""Cheap, shared admission for automatic memory; explicit lookup stays broad.

This is evidence of lexical relevance, not confidence in a claim's truth.
Project/authority/negation filtering remains the existing selector's job.
"""
import re
from .context_retrieval import specific_terms, interpret_request

# Communication/outcome words cannot identify the subject of a past fact.
# This list complements, rather than changes, the legacy consumer vocabulary.
_NOISE = frozenset('成功 失败 完成 搞定 好了 可以 应该 需要 必须 已经 还是 '
    '怎么 为什么 有没有 有没 是否 到底 之前 以后 最近 现在 这次 那次 '
    '一下 一直 总是 总结 看看 继续 谢谢 帮我 我们 你们 这个 那个 '
    '真的 半天 都没 没成 功么 有什 么问 些什'.split())


_FEEDBACK_PREFIX = tuple(sorted(('为什么 怎么 是否 到底 这次 现在 最近 总是 已经 一直 '
    '又 还 都 搞了 搞 忙了 这么久 这么 半天 了 没 没有 没能 未 不 能 不能 就 是 有 才 的 这 都不').split(),key=len,reverse=True))
_FEEDBACK_SUFFIX = ('没有','了','吗','么','呢','啊','吧','没')

# Only anchored request frames are stripped; verbs inside a name stay intact.
_REQUEST_PREFIX = re.compile(
    r'^(?:(?:请问|请|麻烦|帮我|帮忙|看一下|看下|看看|读一下|读下|读读|'
    r'查一下|查下|查查|了解一下|核对|查看|告诉我|关于)\s*)+')


def execution_steering(query):
    """Pure pacing/delegation commands do not request historical knowledge.

    Match complete clauses, not words inside a substantive question. A request
    to explain parallel agents, for example, is not swallowed by this gate.
    """
    clauses=[x.strip() for x in re.split(r'[，,。；;！!\n]+',query) if x.strip()]
    patterns=(r'(?:请|麻烦)?(?:加速|加快|快点|赶紧)(?:一点|些|执行|推进|干|做|搞|吧|啊)*',
        r'(?:请|麻烦)?继续(?:执行|推进|干|做|搞)(?:吧|啊)?',
        r'(?:请|再|多)?(?:开|用|搞|找|派|安排|启动|增加)(?:几个|一些|几位|更多|多个)?'
        r'(?:agent|agents|代理|智能体|协作者)(?:来|一起|同时|并行|协同|合作|参与|干|做|搞|执行|推进|处理|吧|啊|点|些|个)*',
        r'(?:请|大家|你们)?(?:同时|一起|并行|协同)(?:干|做|搞|执行|推进|处理)(?:吧|啊)?')
    return bool(clauses) and all(any(re.fullmatch(p,c,re.I) for p in patterns) for c in clauses)


def _asserted_view(text):
    # A disclaimer about another object/state must not manufacture positive
    # applicability to it. Ordinary negative constraints (不得/不能/不支持)
    # remain intact and are still useful evidence.
    return '；'.join(part for part in re.split(r'[，,。；;\n]+',text)
        if not re.match(r'^\s*(?:这|此|该记录|该条|该规则|资料|记录|本文|它)?\s*'
                        r'(?:不是|并非|不代表|不涉及|未涉及|不包含|未包含)',part))


def _request_states(text):
    negated_success=r'(?:未|不|没(?:有)?)(?:能|曾)?成功'
    failed=bool(re.search(r'失败|'+negated_success+r'|没(?:能)?(?:送出|发出|通过|完成)|等待重发|重试队列',text))
    positive=re.sub(negated_success,'',text)
    succeeded=False;uncertain=False
    for clause in re.split(r'[，,。；;！？?!\n]|但是|然而|而是',positive):
        for outcome in re.finditer(r'成功|已完成|已通过',clause):
            # An earlier negation can reach this outcome through arbitrary
            # verbs/modifiers. Without a full scope parse, do not assert
            # success or guess failure. Later negation does not retroactively
            # negate an already asserted outcome in this clause.
            if re.search(r'没(?:有)?|未|不',clause[:outcome.start()]):uncertain=True
            else:succeeded=True
    night=bool(re.search(r'夜间|夜晚|夜里|夜航|夜班',text))
    day=bool(re.search(r'日间|白天|昼间',text))
    return (set() if uncertain else {'failed'} if failed and not succeeded else {'succeeded'} if succeeded and not failed else set(),
            {'night'} if night and not day else {'day'} if day and not night else set())


def applicable(query, body, heading=''):
    """Reject explicit state conflicts; missing literal words prove no conflict.

    A Chinese phrase before 的 can contain a predicate or multiple concepts,
    and related evidence can paraphrase it. Entity identity still uses the
    established scoped selector; this shared guard does not invent identities
    by treating an arbitrary possessive phrase as an exact-match identifier.
    """
    if execution_steering(query):return False
    claim=_asserted_view(heading+'。'+body)
    for requested, offered in zip(_request_states(query),_request_states(claim)):
        if requested and offered and requested.isdisjoint(offered):return False
    return True


def filter_applicable(cards,query,project=''):
    """One route-independent conflict pass over already scoped candidates.

    Repetition is not an exact-match contract: two requested subquestions can
    have separate evidence, and either can use a synonym. Candidate ranking
    and verified basis relations decide relevance, not repeated query bigrams.
    """
    return [c for c in cards if applicable(query,c.get('body',''),c.get('title',''))]


# After a name, these words introduce clause syntax rather than a possessed
# aspect. Keep single characters out of this list when they can also begin a
# real aspect (for example, 应用, 能力, 对象 or 并行).
_ASPECT_CLAUSE_PREFIX = tuple(sorted((
    '可以', '应该', '应当', '应怎样', '应怎么', '应如何',
    '能否', '能不能', '是否', '怎么', '怎样', '如何',
    '为什么', '为何', '有哪些', '哪些', '什么', '有没有', '有没',
    '选哪些', '选择哪些', '过程中',
), key=len, reverse=True))


def _conversational_name_tail(phrase):
    """Whether Chinese after a name belongs to its enclosing clause.

    "N 里用", "N 时可以" and "A 与 B" do not invent facets. Closed
    prepositional objects are parsed separately before this fallback. The
    specific time and scope patterns avoid discarding genuine aspects such
    as 时钟, 上下文, 内存, 在线 or 里程碑.
    """
    if phrase in ('与', '和', '跟', '及', '以及', '或', '或者') or phrase.startswith(_ASPECT_CLAUSE_PREFIX):
        return True
    if re.match(
            r'^时(?:$|候|可以|可|能|应该|应当|应|要|需要|需|怎样|怎么|如何|是否|有|我|你|请)',
            phrase):
        return True
    if re.match(
            r'^(?:里|中|内|上|下)(?:$|用|使用|运行|部署|调用|配置|选择|接入|安装|操作|安排|可以|可|能|应该|应当|应|怎样|怎么|如何)',
            phrase):
        return True
    return False


# Parse only bounded, explicit clause frames. The object stays a relevance
# constraint; recognizing a preposition must not admit every same-name note.
_ASPECT_BOUNDARY = (
    r'的|怎么|怎样|如何|是否|有什么|有没有|能否|是什么|为什么|为何|'
    r'应该|应当|应怎样|应怎么|应如何|可以|能够|需要|运行|部署|使用|'
    r'配置|接入|安排|选择|调用'
)
_NOUN_DETERMINERS = r'(?:(?:个人|自己的|我们的|这个|那个|该))*'
_CLAUSE_NOUN = r'(?:(?!' + _ASPECT_BOUNDARY + r')[\u3400-\u9fff]){2,24}?'
_LOCATIVE_OBJECT = re.compile(
    r'^(?:在|于)' + _NOUN_DETERMINERS + '(' + _CLAUSE_NOUN + r')(?:里|中|内|上|下)(?=$|' + _ASPECT_BOUNDARY + r')')
_PREPOSITION_OBJECT = re.compile(
    r'^(?:通过|借助|按照|根据|针对|关于|对于)' + _NOUN_DETERMINERS + '(' + _CLAUSE_NOUN + r')(?=$|' + _ASPECT_BOUNDARY + r')')
_CONDITIONAL_CONTENT = re.compile(
    r'^当([\u3400-\u9fff]{2,24}?)时(?=$|' + _ASPECT_BOUNDARY + r'|需|要)')


def _name_tail_aspect(phrase):
    """Extract a direct aspect or a closed prepositional object's noun.

    Possessive/demonstrative determiners do not become the aspect (个人技能库
    constrains 技能库). This is a small syntax rule, with no product, material
    or aspect-name exceptions. Unclosed/ambiguous clauses retain the original
    lexical constraint rather than dropping it and widening admission.
    """
    # A closed condition carries substantive content, not just syntax. Never
    # erase it like a bare modal connector. Purpose clauses without a parsed
    # object (为了...) likewise retain their original lexical constraint.
    for pattern in (_LOCATIVE_OBJECT, _PREPOSITION_OBJECT, _CONDITIONAL_CONTENT):
        match = pattern.match(phrase)
        if match:
            return match[1]
    return '' if _conversational_name_tail(phrase) else phrase



def _consume(text, offset, words):
    while offset<len(text):
        word=next((word for word in words if text.startswith(word,offset)),None)
        if word is None:break
        offset+=len(word)
    return offset


def _feedback_only(text):
    # Deterministic longest-token scan: overlapping 没/没有/有 must not cause
    # exponential regex backtracking on a long, nonmatching user message.
    text=text.strip(' ？?！!。\t\r\n')
    if re.fullmatch(r'(?:这个|这次|现在|最近|那个|感觉)?(?:效果|结果)(?:不太好|不好|很差|不错|很好|怎么样|如何)',text):
        return True
    pos=_consume(text,0,_FEEDBACK_PREFIX)
    outcome=next((w for w in ('成功','完成','搞定','解决','失败') if text.startswith(w,pos)),None)
    return bool(outcome and _consume(text,pos+len(outcome),_FEEDBACK_SUFFIX)==len(text))


def query_terms(query, project=''):
    text=interpret_request(query).text
    if len(project)>=2:
        boundary=r'\w' if re.search(r'[\u4e00-\u9fff]',project) else 'A-Za-z0-9_'
        text=re.sub(r'(?<!['+boundary+'])'+re.escape(project)+r'(?!['+boundary+'])', ' ', text, flags=re.IGNORECASE)
    if _feedback_only(text) or execution_steering(query):
        return set()
    return specific_terms(text) - _NOISE


def _leading_subject(query):
    # Chinese tokenization creates cross-word bigrams: 封签怎么验收 contains
    # 签怎/么验. Those fragments must not outvote the explicit noun 封签.
    text=interpret_request(query).text.strip()
    text=_REQUEST_PREFIX.sub('',text)
    match=re.match(r'^([\u4e00-\u9fff]{2,32}?)(?:的|怎么|如何|有什么|是否|有没有|能否|验收|部署|限制|要求|条件|规则|流程|[？?])',text)
    subject=match.group(1) if match else ''
    return subject if subject and subject not in _NOISE else ''


def named_query_terms(query, project=''):
    """Complete named tokens, also when adjacent to Chinese (DS缓存)."""
    return {term for term in query_terms(query, project)
            if re.fullmatch(r'[a-z][a-z0-9_]*', term, re.I)}


def _named_aspect_matches(query, body, project='', heading='', subject_heading=None):
    """Conjoin the named subject and its immediate requested aspect.

    Broad same-name expansion must not turn a cache question into video
    authorization. Existing tokenization supplies names; the Chinese phrase
    immediately after a name, or a bounded prepositional object there,
    supplies the aspect. A verb before the name ("部署 TinyCore") adds no
    fake facet.
    """
    if not applicable(query,body,heading):return False
    names = named_query_terms(query, project)
    if not names:
        return True
    head = specific_terms(heading)
    body_terms = specific_terms(body)
    if not names & (head | body_terms):
        return False
    # Use the human title for this identity check, not opaque storage keys.
    # A Kimi/DSH title cannot become a DS note through incidental body mentions.
    titled_names = named_query_terms(heading if subject_heading is None else subject_heading, project)
    if titled_names and not names & titled_names:
        return False
    facets = set()
    text = interpret_request(query).text
    # Formatting around a named token is not a semantic boundary between the
    # name and its aspect (`NX` 索引 and NX 索引 ask the same question).
    text = re.sub(r'(`+|\*\*|__)([A-Za-z][A-Za-z0-9_]*)\1', r'\2', text)
    for match in re.finditer(r'[A-Za-z][A-Za-z0-9_]*(?:\.\d+)*', text):
        if match[0].split('.', 1)[0].lower() not in names:
            continue
        tail = re.match(r'\W*([\u3400-\u9fff]+)', text[match.end():])
        if tail:
            raw_phrase = _name_tail_aspect(tail[1].lstrip('的'))
            phrase = re.split(r'的|怎么|如何|是否|有什么|有没有|能否|是什么', raw_phrase, maxsplit=1)[0]
            wanted = query_terms(phrase)
            # Preserve the leading aspect, rather than accepting a later
            # shared outcome word (cache hit vs search hit).
            first = next((phrase[i:i+2] for i in range(len(phrase)-1)
                          if phrase[i:i+2] in wanted), None)
            if first:
                facets.add(first)
    if not facets:
        return True
    if not facets & (head | body_terms):
        return False
    if names & head:
        return True
    # Without a subject in the title, require one local statement to supply
    # both subject and aspect; unrelated paragraphs cannot be combined.
    return any(names & specific_terms(part) and facets & specific_terms(part)
               for part in re.split(r'[\n。！？!?；;]+', body))


def relevant(query, body, project='', heading=''):
    wanted=query_terms(query,project)
    if not wanted or not _named_aspect_matches(query,body,project,heading):
        return False
    head=specific_terms(heading)-_NOISE
    if wanted & head:
        return True
    subject=_leading_subject(query)
    if subject and subject in body:
        return True
    overlap=wanted & (specific_terms(body)-_NOISE)
    # A focused one-term query (e.g. SQLite / 封签) is useful. A long query
    # must share more than an accidental body word with the proposed evidence.
    if any(re.fullmatch(r'[a-z][a-z0-9_.-]{2,}', term, re.I) for term in overlap):
        return True
    return bool(overlap) if len(wanted)==1 else (
        len(overlap)>=2 and len(overlap)/min(len(wanted),8)>=0.25)


def filter_notes(cards, query, project):
    cards=list(cards)
    wanted=query_terms(query,project)
    heading=lambda c:' '.join(str(c.get(k) or '') for k in ('title','key','topic'))
    # The upstream selector already expands established subjects into other
    # cards' bodies. Preserve their conditions even when the query also has an
    # action word ("deploy TinyCore" vs "TinyCore permits one writer only").
    subjects=wanted & set().union(*(specific_terms(heading(c)) for c in cards))
    return filter_applicable([c for c in cards if _named_aspect_matches(query,c.get('body',''),project,heading(c),
                                                    subject_heading=c.get('title',''))
            and ((subjects & specific_terms(c.get('body','')))
                 or relevant(query,c.get('body',''),project,heading(c)))],query,project)
