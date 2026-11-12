"""Deterministic consumer-side relevance gating for context injection.

This is deliberately separate from the maintenance recall primitives in
``evidence_selection`` (``rank`` / ``lexical_hits`` / ``search_page``), so
consumer gating can be tuned without touching the deterministic guards used
before a batch is published.

Rules (all deterministic, no corpus statistics, no learned thresholds):

* An own-project card may be admitted through an exact identity, a heading
  overlap or a *specific* term, but project equality on its own is not topical
  evidence: a raw/generic body overlap never admits a card.
* A shared card is admitted only on a *specific* term -- never on a lone
  generic word in its heading, title, body or via a substring of a longer
  generic title. This holds even though a home cwd may contain many domains
  (差旅/招聘/部署 ...).
* A query made only of very common topical words (for example ``方案``) may
  still surface *own-project* cards whose heading actually carries that word,
  but never an unrelated shared card.
* A zero-overlap card is never injected, so unrelated memory cards cannot crowd
  out ordinary knowledge that carries positive specific relevance.
* A precise explicit named subject (an English identifier or acronym such as
  ``FinchCore``/``MQTT``) outranks the generic action words it may share with a
  *different* subject: only cards naming that complete subject are admitted, so
  ``卡片盒产品化`` cannot answer a ``FinchCore 产品化升级`` question. Recognition is
  case-insensitive for a complete token that actually names a card heading
  (``finchcore`` is the same subject as ``FinchCore``), while an ordinary English
  word or a format request is never promoted to a subject. Several named
  subjects are kept together, and an explicit *unknown* subject never falls back
  to an unrelated generic card.
* When a strong current subject match exists, a weak card that merely shares one
  generic body term does not ride along; a full-body card that names the *same*
  subject remains usable.
* Short acknowledgements (``好``/``好的``/``谢谢``/``收到`` ...) yield nothing.
* A longer turn that explicitly asks for no historical/project memory this turn
  (``先暂停调取旧记录``) or for a bare acknowledgement only (``只需回一句已知悉``)
  yields nothing -- for the selected cards *and* the raw fallback/session carry
  -- while an explicit present subject still wins (``不用复述Beta旧方案，请解释
  Gamma防漏检测`` keeps Gamma). The signal is a bounded frame, not a substring:
  a bare ``收到``/``确认`` or quoted source text never disables; an ``only``-marker
  + ack predicate must exhaust its clause except bounded particles/fillers (so
  ``只需知道白榆测绘队结果`` keeps its content); a negation embedded in a
  ``为什么``/``为何`` policy discussion is not a directive; and a clause mixing an
  ack request with a memory negation (``现在仅作收件确认，不引用旧决定``) is a
  deferment, not a positive question.
* An explicit continuation (``继续``/``下一步`` ...) or a *referential follow-up*
  may recover the bounded same-session subject, or -- when there is no usable
  anchor -- a small project-status (own ``memory``) set. Only a *pure*
  continuation frame, a *generic, question-shaped* referral with no concrete
  remainder, or a turn that opens with a third-person/deictic subject pronoun
  qualifies: addressing the assistant (``你``/``您``) or being short is never
  enough, an explicit topic-switch phrase disables carry, and a fresh concrete
  subject never carries the previous one. Bounded self-reference, lookup and
  subjective-appraisal vocabulary (``自己``/``查询``/``感觉``/``看``) belongs to
  the frame, so an ordinary reflexive assistant-behaviour or appraisal question
  reduces to no concrete remainder and refers, while a concrete noun
  (``你自己会查询海豚迁徙吗``) still leaves a remainder and never carries.
"""

from __future__ import annotations

from collections import namedtuple
import re

from .context_query import discussion_text
from .evidence_selection import terms

# Fixed, bounded stopword policy for the *consumer* path only. These are very
# common topical words with little discriminating power. Nothing here is
# learned from test answers or from the corpus.
CONSUMER_GENERIC = {
    "方案", "计划", "项目", "问题", "情况", "状态", "内容", "效果", "交互",
    "准备", "需求", "设计", "开发", "测试", "验证", "工作", "流程", "功能", "方式",
    "方法", "结果", "总结", "说明", "文档", "系统", "数据", "信息", "时间",
    "用户", "卡片", "知识", "推送", "开头", "每轮", "最新", "相关", "进行",
    "继续", "当前", "以后", "现在", "一些", "一下", "如何", "怎样", "什么",
}

# Continuation and acknowledgement vocabularies. Kept tiny and explicit.
_CONTINUATION_MARKERS = (
    "继续", "接着", "下一步", "接下来", "然后呢", "然后", "还有呢", "还有什么",
    "怎么办", "往下", "后续", "再来", "下一步做", "接着说", "继续做",
)

# Single-character/word acknowledgements, compared after stripping whitespace
# and common punctuation. A short all-ack string (for example 好的谢谢) also
# counts; a string carrying any other idea does not.
_ACK_WORDS = {
    "好", "好的", "好滴", "好吧", "好勒", "行", "行的", "嗯", "嗯嗯", "嗯好",
    "谢谢", "谢谢你", "多谢", "感谢", "收到", "收到啦", "了解", "明白了",
    "知道了", "知悉", "辛苦", "辛苦了", "ok", "okay", "知道", "明白",
}
_ACK_CHARS = set("好谢收到辛苦了解明白嗯行滴的啦多谢你了")

_PUNCT = re.compile(r"[\s，。！!？?、~～.．…·:：;；\"'“”‘’()（）\[\]【】]+")


def normalise_query(text):
    if not isinstance(text, str):
        return ""
    return _PUNCT.sub("", text).strip().lower()


def is_acknowledgement(text):
    """Whether the whole turn is a short acknowledgement with no topic."""
    stripped = normalise_query(text)
    if not stripped:
        return False
    if stripped in _ACK_WORDS:
        return True
    # A short run of acknowledgement characters only (e.g. 好的谢谢).
    if len(stripped) <= 8 and set(stripped) <= _ACK_CHARS:
        return True
    return False


# ---------------------------------------------------------------------------
# Turn-level request interpretation (consumer path only).
#
# One bounded, *clause-level* reading of how a turn wants to be answered. It
# replaces the earlier whole-turn substring scan, which could not tell an
# unscoped "do not fetch memory" request from a *scoped* negation of one old
# subject, and which read a bare ``回一句`` / ``无需展开`` as a global disable
# even though a short factual answer still needs evidence.
#
# The separated discussion is split on sentence/clause separators and each
# clause gets one role: ``ack`` (an acknowledgement-only request), ``negate``
# (a negation of historical/project memory) or ``content``. A quoted /
# backticked / fenced span is inert *for classification* (a pasted source or a
# discussed protocol is data, not a command) but the original clause is never
# mutated, so a quoted subject discussion stays meaningful downstream.
# ---------------------------------------------------------------------------

# Explicit requests to stop drawing on historical / project memory this turn.
_MEMORY_ABSTENTION_CUES = (
    "暂停调取", "暂停检索", "暂停查询", "暂停调用", "暂停取用", "暂停回顾",
    "不要调取", "不用调取", "无需调取", "别调取",
    "不要检索", "不用检索", "无需检索", "别检索",
    "不要查询", "不用查询", "无需查询", "别查询",
    "不要回顾", "不用回顾", "无需回顾", "别回顾",
    "不要复述", "不用复述", "无需复述", "不必复述", "别复述",
    "忽略历史", "忽略旧记录", "忽略旧资料", "忽略背景",
    "不要旧记录", "不用旧记录", "别用旧记录",
    "不用翻旧", "不要翻旧", "别翻旧",
    "不要记忆", "不用记忆", "别用记忆",
    "暂停使用记忆", "暂停调用记忆", "暂停调用资料",
    "不要引用", "不用引用", "无需引用", "别引用", "不必引用",
    "不要参考", "不用参考", "无需参考", "别参考", "不必参考",
    "不要提及", "不用提及", "无需提及", "别提及",
)

# A negation particle followed (in a tiny window) by a memory/retrieval verb is
# also a memory negation: ``不要引用旧资料`` / ``别参考旧方案`` / ``别用旧记录``.
# For the *generic* verbs (引用/参考/使用/用/带) a memory object must stand in the
# same clause, so an ordinary ``不要使用红色`` is never read as an abstention.
_NEGATION_PARTICLE_RE = re.compile(
    r'(?:不要|不用|无需|不必|别|请勿|勿|停止|暂停|忽略)(?:再|又|重新|重复|继续)?')
_SPECIFIC_MEMORY_VERBS = (
    "调取", "检索", "查询", "调用", "取用", "回顾", "复述", "翻查", "翻阅",
    "查阅", "加载", "拉取", "召回", "回溯", "读取", "提及", "翻旧", "回看",
    "接续",
)
_GENERIC_MEMORY_VERBS = ("引用", "参考", "使用", "用", "带", "引", "参")
_MEMORY_OBJECTS = (
    "旧", "之前", "以前", "先前", "过往", "历史", "原来", "原", "方案", "计划",
    "记录", "资料", "背景", "记忆", "文档", "卡片", "聊天", "会话", "上下文",
    "知识库", "资料库", "材料", "内容",
)

# A bare ``不`` that *directly* negates a memory verb (``不引用旧决定`` /
# ``先不接续旧话题`` / ``不再复述旧方案``) is the same bounded negation as
# ``不要引用``. The particle must be *directive*: it starts the clause or follows
# a bounded scope adverb (先/暂/暂时/也/都/就/再), so an embedded interrogative
# such as ``为什么不参考旧资料`` is never read as an abstention. The verb must
# follow the particle directly, with only an optional 再/又/重新/重复/继续 in
# between; a *generic* verb still needs a memory object in the clause.
_BARE_NEG_AUX = r'(?:再|又|重新|重复|继续)?'
_BARE_NEG_SCOPE = (r'(?:^[ \t]*|(?<=先)|(?<=暂)|(?<=暂时)|(?<=也)'
                   r'|(?<=都)|(?<=就)|(?<=再))')
_BARE_NEG_SPECIFIC_RE = re.compile(
    _BARE_NEG_SCOPE + r'不' + _BARE_NEG_AUX + r'(?:'
    + '|'.join(re.escape(verb) for verb in _SPECIFIC_MEMORY_VERBS) + r')')
_BARE_NEG_GENERIC_RE = re.compile(
    _BARE_NEG_SCOPE + r'不' + _BARE_NEG_AUX + r'(?:'
    + '|'.join(re.escape(verb) for verb in _GENERIC_MEMORY_VERBS) + r')')

# A bare reply/quality-limit frame is *not* by itself a disable: a short factual
# answer can still require evidence. It only counts next to an actual ack
# predicate -- so ``请只确认收到`` and ``只需回一句已知悉`` are the same intent,
# while ``回一句结论`` / ``无需展开`` alone are not. "No reply needed" disables
# on its own.
_ACK_PREDICATES = (
    "已知悉", "知悉", "收到", "确认", "明白", "了解", "知道", "好的",
    "可以", "没问题", "感谢", "多谢", "谢谢", "辛苦",
)
_ACK_REPLY_FRAMES = (
    "只需回", "只要回", "只回", "需回", "回一句", "回个", "答一句", "只需答",
    "只需一句", "只一句", "只需确认", "只要确认", "只确认", "确认即可",
    "确认一下即可", "知道就行", "知道了就行", "知道即可", "知悉即可",
    "无需展开", "不用展开", "不必展开", "无需赘述", "不用赘述",
    "无需多言", "不用多说",
)
# Confirm-frames already carry their ack predicate (``只确认`` / ``确认即可`` /
# ``知道即可`` ...), so whatever *follows* the frame inside the same clause must
# itself be only bounded ack/filler material: ``请只确认收到`` is ack-only,
# while ``只要确认白榆测绘队协议没问题`` (a concrete subject after the frame)
# is genuine content.
_ACK_CONFIRM_FRAMES = (
    "只需确认", "只要确认", "只确认", "确认即可", "确认一下即可",
    "知道就行", "知道了就行", "知道即可", "知悉即可",
)
_ACK_SUFFIX_RE = re.compile(
    r'(?:[\s，,。！!？?、:：]|一下|一句|一个|个|声|了|吧|呢|啊|哦|嘛|的|就|也'
    r'|即可|就行|已知悉|知悉|收到|收件确认|确认|明白|了解|知道|没问题|可以|好的'
    # Bounded receipt-confirmation constructions: ``只要确认消息已经收到`` is
    # the same ack-only intent as ``请只确认收到``. The full-match requirement
    # still rejects any concrete subject before the phrase (``只要确认白榆测绘队
    # 协议没问题`` stays content).
    r'|消息已经收到|消息已收到|已经收到|已收到)*$')
# The remaining reply frames stay a loose substring frame, except that ``只回``
# must not match inside the content verb ``只回答`` (a loose ``只回`` substring
# never matches ``只回答`` plus a distant ``知道``).
_ACK_REPLY_FRAME_RE = re.compile('|'.join(
    ('只回(?!答)' if frame == '只回' else re.escape(frame))
    for frame in _ACK_REPLY_FRAMES if frame not in _ACK_CONFIRM_FRAMES))
_NO_REPLY_FRAMES = (
    "无需回复", "不用回复", "不必回复", "请勿回复", "免回复", "无需回覆",
    "不用回覆",
)

# A bounded "only / just" request marker followed by an actual acknowledgement
# predicate is a *nominal* acknowledgement request (``现在仅作收件确认`` /
# ``只作确认``), the same intent as ``只需回一句已知悉``. The marker and the
# predicate must sit next to each other (only an optional 收件/收到/一句/一下/
# 个/声 filler and separators between), so an ordinary discussion such as
# ``收件确认流程怎么做`` (no marker) or ``只需说明确认协议`` (a content verb between)
# is never read as a bare-ack request. Quoted spans stay inert for
# classification via :func:`_command_inert`.
_ACK_ONLY_MARKERS = (
    "仅作", "只作", "仅做", "只做", "仅需", "只需", "仅要", "只要",
)
_ACK_ONLY_FILLERS = ("收件", "收到", "一句", "一下", "个", "声")
# Deliberately narrower than ``_ACK_PREDICATES``: only unambiguous
# acknowledgement words, so ``只需好的方案`` / ``只要可以`` are not mistaken for a
# bare-ack request.
_ACK_ONLY_PREDICATES = (
    "已知悉", "知悉", "收到", "收件确认", "确认", "明白", "了解", "知道",
)
_ACK_ONLY_RE = re.compile(
    r'(?:' + '|'.join(re.escape(m) for m in _ACK_ONLY_MARKERS) + r')'
    r'[\s:：=，,]*'
    r'(?:' + '|'.join(re.escape(f) for f in _ACK_ONLY_FILLERS) + r')?'
    r'[\s:：=]*'
    r'(?:' + '|'.join(re.escape(p) for p in _ACK_ONLY_PREDICATES) + r')')
# Bounded continuations that still belong to an ack predicate: ``没问题`` /
# ``可以`` / ``好的`` directly following ``确认`` / ``知道`` / ``收到`` (so
# ``只要确认白榆测绘队协议没问题`` is content, while ``只需确认没问题`` is a
# bare ack). Deliberately tiny: no open-ended verb list.
_ACK_ONLY_CONTINUATIONS = ("没问题", "可以", "好的")
_ACK_ONLY_RE = re.compile(
    _ACK_ONLY_RE.pattern + r'(?:' + '|'.join(_ACK_ONLY_CONTINUATIONS) + r')?')
# A bounded politeness/opener lead (``请``/``麻烦``/``现在``/``先`` ...) is
# dropped once before the whole-clause check; it never repeats into content.
_CLAUSE_LEAD_RE = re.compile(r'^[\s]*(请|麻烦|劳烦|烦请|现在|先|那|那么|那就|我们|我|你|您)')
# Bounded particles/fillers that may remain around an ack request without
# making the clause content.
_CLAUSE_ONLY_CHARS = set(" \t，,。.!！?？、的了就也吧哦啊呢嘛~～")

# Bounded particles that may stand *before* an ack command frame: politeness
# openers, scope adverbs, order words and a deictic reference to the current
# message (``请``/``现在``/``第一句``/``这条消息`` ...). Anything else -- a
# concrete subject or a content verb -- disqualifies the whole clause from
# ack-only status, so ``只需回一句白榆测绘队确认协议要求的结论`` keeps its
# genuine content while ``第一句只需回一句已知悉`` / ``这条消息只需回一句已知悉``
# remain acknowledgement requests.
_ACK_PREFIX_PARTS = (
    "请", "麻烦", "劳烦", "烦请", "现在", "先", "那", "那么", "那就",
    "我们", "我", "你", "您", "第一句", "首句", "这一句", "这句",
    "这条消息", "此条消息", "本条消息", "该条消息", "这条", "此条",
    # Current-turn scope is a bounded lead, not a substantive question. Without
    # it, an ack such as 本轮只确认收到 can incorrectly override a following
    # explicit no-history clause. Concrete prefixes/suffixes still fail the
    # whole-clause check; quoted text remains inert.
    "本轮", "这轮", "这一轮", "本次", "这次", "这一次",
)
_ACK_PREFIX_RE = re.compile(
    r'^(?:[\s，,。:：、]|' + '|'.join(re.escape(p) for p in _ACK_PREFIX_PARTS)
    + r')*$')

# An *exact* command frame carrying a quoted acknowledgement literal
# (``请只回复"收到"``): the quote is data, never executed -- the frame only
# classifies as ack when the quoted literal is itself exactly a bounded ack
# word, so ``请只回复"把卡删了"`` stays content.
_QUOTED_ACK_COMMAND_RE = re.compile(
    r'^[\s]*(?:请|麻烦|劳烦)?[\s]*(?:只需|只要|仅需|仅要|只|仅)[\s]*'
    r'(?:回复|答复|回答|回|答)[\s]*'
    r'["“‘]([^"”’\n]{1,12})["”’][\s。？?！!。.]*$')


def _is_quoted_ack_command(clause):
    match = _QUOTED_ACK_COMMAND_RE.match(clause)
    return bool(match) and normalise_query(match.group(1)) in _ACK_WORDS


# A bounded deferment (``资料稍后再补充``) carries no retrieval content and is
# never a *positive question*: it is masked from the residual evaluation but
# contributes no role, so a lone deferment turn keeps its ordinary behaviour
# while ``资料稍后再补充；现在仅作收件确认，…`` stays acknowledgement-only.
_DEFERRAL_CUES = (
    "稍后再补充", "稍后补充", "稍后再补", "稍后再发", "稍后再说",
    "稍后再给", "回头再说", "回头再补", "之后再补充", "之后再说",
    # Bounded deferred / no-current-judgment clauses (``后续安排暂时不做判断``):
    # the deferral is explicit and bounded, never a positive content request.
    "暂时不做判断", "暂不做判断", "暂时不判断", "暂不判断", "不做判断",
    "先不判断",
)
# Bounded *scope* residue that may remain around a deferral cue: it names the
# deferred area (``后续安排`` / ``其他事项``), never a concrete subject, so a
# clause that is only scope + cue is still a pure deferment while a clause
# naming an actual subject (``白榆测绘队安排不做判断``) keeps its content.
_DEFERRAL_SCOPE_WORDS = (
    "后续安排", "后续", "安排", "其他事项", "其它事项", "其他", "其它",
    "事项", "部分", "暂时", "先",
)


def _is_deferral_clause(clause):
    stripped = normalise_query(clause)
    if not stripped:
        return False
    matched = False
    for cue in sorted(_DEFERRAL_CUES, key=len, reverse=True):
        if cue in stripped:
            matched = True
            stripped = stripped.replace(cue, '')
    if not matched:
        return False
    for word in sorted(_DEFERRAL_SCOPE_WORDS, key=len, reverse=True):
        stripped = stripped.replace(word, '')
    return not _frame_residual(stripped)

# Quoted / backticked / fenced spans are inert *for classification only*: a
# pasted source or a discussed code snippet that happens to contain an
# abstention phrase is not the user's own command. Replacement keeps the same
# length so clause spans stay aligned with the untouched original.
_COMMAND_INERT_RE = re.compile(
    r'```.*?(?:```|$)|~~~.*?(?:~~~|$)|`[^`]*`|"[^"\n]*"|“[^”]*”|‘[^’]*’',
    re.S)
# Sentence/clause separators used only to give each clause one role. Scanning
# the *original* text and slicing the same-length inert view keeps a quoted span
# that happens to contain a separator correctly blanked.
_CLAUSE_DELIMS = frozenset('。！？；\n，,;!?')

# Backwards-compatible aliases (older call sites/tests).
_QUOTED_SPAN_RE = re.compile('`[^`]*`|"[^"\n]*"|“[^”]*”')
_ACK_ONLY_CUES = _ACK_REPLY_FRAMES


def _command_inert(text):
    """Blank fenced/quoted spans in place (same length) for classification."""
    return _COMMAND_INERT_RE.sub(lambda m: ' ' * len(m.group(0)), text)


def _whole_clause_is_ack_only(clause):
    """Whether the clause is *only* a bounded acknowledgement request.

    The marker + ack-predicate match must be exhaustive: what remains after
    removing a leading politeness opener and the matched request span may only
    be bounded particles/fillers (``一下``/``个``/``吧`` ...) or punctuation --
    never a concrete subject. So ``请只确认收到`` / ``只需回一句已知悉`` /
    ``现在仅作收件确认`` are ack-only, while ``只需知道白榆测绘队结果`` /
    ``只要确认白榆测绘队协议没问题`` / ``请只回答你知道的白榆测绘队部分`` /
    ``我仅需收到白榆测绘队文件后再归档`` keep their genuine content (a content
    word between marker and predicate already breaks ``_ACK_ONLY_RE``'s tight
    frame; the whole-clause check then rejects the leftover subject). A loose
    ``只回`` substring never matches across a distant ``知道``.
    """
    match = _ACK_ONLY_RE.search(clause)
    if not match:
        return False
    if _CLAUSE_LEAD_RE.match(clause, match.end()):
        return False
    rest = clause[:match.start()] + clause[match.end():]
    rest = _CLAUSE_LEAD_RE.sub('', rest)
    # The residual test is a whole-string character check, never ``str.strip``
    # (whose argument must be a string, not a set).
    return all(ch in _CLAUSE_ONLY_CHARS for ch in rest)


def _is_ack_only_clause(clause):
    """Whether a clause is *only* a bounded acknowledgement command.

    Every ack branch (confirm frame, reply frame, nominal only-marker) must be
    exhaustive: a bounded lead (politeness/order/deictic particle) before the
    frame and only bounded ack/filler material after it, with an actual ack
    predicate present. A substantive prefix or suffix
    (``只需回一句白榆测绘队确认协议要求的结论`` /
    ``请只回复白榆测绘队确认协议中的关键条件``) keeps the clause genuine
    content, while ``第一句只需回一句已知悉`` / ``请只确认收到`` stay ack.
    """
    if not clause.strip():
        return False
    if any(frame in clause for frame in _NO_REPLY_FRAMES):
        return True
    for frame in _ACK_CONFIRM_FRAMES:
        idx = clause.find(frame)
        if (idx >= 0
                and _ACK_PREFIX_RE.fullmatch(clause[:idx])
                and _ACK_SUFFIX_RE.fullmatch(clause[idx + len(frame):])):
            return True
    match = _ACK_REPLY_FRAME_RE.search(clause)
    if match:
        return (_ACK_PREFIX_RE.fullmatch(clause[:match.start()])
                and _ACK_SUFFIX_RE.fullmatch(clause[match.end():])
                and any(pred in clause for pred in _ACK_PREDICATES))
    return _whole_clause_is_ack_only(clause)


# A memory negation is *directive*: the particle opens the command (possibly
# after a bounded scope adverb such as 先/也). The same words inside a
# ``为什么``/``为何`` interrogative are a policy discussion (``请解释为什么不要引用
# X非正式来源``), never a directive. The guard only applies when the opener
# actually *contains* the first negation particle, so a clause that merely
# mentions 为什么 before an independent directive (``说说为什么不能…；别引用旧资料``)
# still sees its own negation.
_NEGATION_DISCUSSION_OPENERS = ("为什么", "为何")


def _is_memory_negation_clause(clause):
    if not clause.strip():
        return False
    particle_at = min(
        (clause.find(p) for p in
         ('不要', '不用', '无需', '不必', '别', '请勿', '勿', '停止', '暂停', '忽略', '不')
         if clause.find(p) >= 0),
        default=-1)
    if (particle_at >= 0 and any(
            clause.find(opener) >= 0 and clause.find(opener) < particle_at
            for opener in _NEGATION_DISCUSSION_OPENERS)):
        return False
    if any(cue in clause for cue in _MEMORY_ABSTENTION_CUES):
        return True
    if _BARE_NEG_SPECIFIC_RE.search(clause):
        return True
    if (_BARE_NEG_GENERIC_RE.search(clause)
            and any(obj in clause for obj in _MEMORY_OBJECTS)):
        return True
    match = _NEGATION_PARTICLE_RE.search(clause)
    if not match:
        return False
    tail = clause[match.end():match.end() + 10]
    if any(verb in tail for verb in _SPECIFIC_MEMORY_VERBS):
        return True
    if any(verb in tail for verb in _GENERIC_MEMORY_VERBS):
        return any(obj in clause for obj in _MEMORY_OBJECTS)
    return False


def request_role_scan(base):
    """``(masked, roles)`` for an already-separated discussion string.

    ``roles`` is the frozenset of clause roles seen (``ack`` / ``negate``); the
    masked text keeps every original clause except that ack-only and negated
    clauses are replaced by spaces, so a caller can drop them from retrieval.
    The input is never mutated, and the scan is idempotent.
    """
    if not isinstance(base, str) or not base:
        return ('' if not isinstance(base, str) else base), frozenset()
    view = _command_inert(base)
    roles = set()
    out = []
    start = 0
    for index, ch in enumerate(base):
        if ch in _CLAUSE_DELIMS:
            _emit_clause(base, view, start, index, roles, out)
            out.append(ch)
            start = index + 1
    _emit_clause(base, view, start, len(base), roles, out)
    return ''.join(out), frozenset(roles)


def _emit_clause(base, view, start, end, roles, out):
    clause = base[start:end]
    clause_view = view[start:end]
    if _is_deferral_clause(clause_view):
        out.append(' ' * len(clause))
    elif ((_is_ack_only_clause(clause_view) or _is_quoted_ack_command(clause))
            and not _is_memory_negation_clause(clause_view)):
        # A clause carrying both roles (``现在仅作收件确认，不引用旧决定``) is a
        # deferment, not a bare acknowledgement: it keeps the ``negate`` role,
        # so a positive question elsewhere in the turn still wins.
        roles.add('ack')
        out.append(' ' * len(clause))
    elif _is_memory_negation_clause(clause_view):
        roles.add('negate')
        out.append(' ' * len(clause))
    else:
        out.append(clause)


def suppresses_current_memory(text):
    """Whether the turn carries an abstention/acknowledgement *role*.

    A bounded per-turn signal: any clause that negates historical memory or
    asks for a bare acknowledgement. Callers that must distinguish a *scoped*
    negation of one old subject (``不用复述Beta旧方案，请解释Gamma防漏检测``)
    from an *unscoped* "no memory" request use the clause roles directly via
    :func:`interpret_request`. Quoted/fenced spans are inert, so a discussed
    protocol or pasted source is never a disable.
    """
    if not isinstance(text, str) or not text:
        return False
    _masked, roles = request_role_scan(discussion_text(text))
    return bool(roles)


# ``text`` is the *retrieval* discussion (ack-only / negated clauses removed);
# ``no_history`` is true for ``ack_only`` and for a negation with no positive
# clause remaining.
RequestInterpretation = namedtuple(
    'RequestInterpretation', ('ack_only', 'no_history', 'text'))


def interpret_request(text):
    """One bounded, deterministic reading of *how* ``text`` wants an answer.

    Splitting the separated discussion into clauses and masking only the
    ack-only / negated ones lets a *scoped* negation of an old subject keep a
    positive current question (``别复述CopperDesk旧方案；现在解释SilverGate
    校准规程。`` keeps SilverGate), while a turn whose remaining clauses carry
    no concrete question (``先暂停调取旧记录`` / ``请只确认收到``) is a global
    no-history request. An acknowledgement request wins outright unless a
    genuine positive question survives after the ack clause.
    """
    base = discussion_text(text) if isinstance(text, str) else ''
    masked, roles = request_role_scan(base)
    has_positive = (specific_terms(masked) and not is_acknowledgement(masked)
                    and _frame_residual(normalise_query(masked)))
    if 'ack' in roles:
        # An acknowledgement request wins outright only when no concrete
        # positive question survives (a deferment such as ``资料稍后再补充；现在
        # 仅作收件确认，不引用旧决定`` is not one); a genuine positive question
        # after the ack clause (``第一句只需回一句已知悉；然后解释X``) is kept.
        if 'negate' in roles or not has_positive:
            return RequestInterpretation(True, True, '')
        return RequestInterpretation(False, False, masked)
    if 'negate' in roles:
        if has_positive:
            return RequestInterpretation(False, False, masked)
        return RequestInterpretation(False, True, '')
    return RequestInterpretation(False, False, base)


# ---------------------------------------------------------------------------
# Bounded, explicit referral / continuation vocabulary.
#
# All lists below are tiny and fixed; none is learned from test answers or from
# the corpus. They let the consumer reason about *structure* -- a genuine
# reference to the current thread versus a fresh concrete subject -- instead of
# treating a second-person address or a short question length as evidence of a
# referral.
# ---------------------------------------------------------------------------

# Explicit "let's change the subject" markers. Presence disables carry even when
# a second-person address or a follow-up word is also present.
_NEW_TOPIC_MARKERS = (
    "换个话题", "换话题", "换个问题", "换个方向", "换个内容", "换个说法",
    "换一个话题", "换一个", "换个", "另一个话题", "另外一个话题", "新话题",
    "新问题", "说点别的", "聊点别的", "讲点别的", "说别的", "聊别的", "别的",
    "不说这个", "不提这个", "先不说这个", "不谈这个", "跳过这个",
)

# An explicit deixis to the immediately preceding turns is, on its own, evidence
# of a referral (``刚才那个怎么样``).
_DEIXIS_CUES = ("上述", "前述", "前面", "之前", "刚才", "刚刚", "上面",
                "前面提到", "刚说", "你说过")

# A referral must be question-shaped, so a bare generic noun (``方案``) is never
# mistaken for a follow-up.
_QUESTION_MARKERS = (
    "吗", "呢", "么", "吧", "呀", "啊", "嘛",
    "怎么", "怎样", "怎么样", "如何", "什么", "为啥", "为什么", "为何",
    "是否", "有没有", "几", "哪", "多少",
)

# Bounded third-person / deictic subject-pronoun openers. A question that opens
# by pointing at the thing under discussion (``它``/``这套``/``那个`` ...) is
# structural evidence of a referral to the *current* subject -- unlike a
# second-person address to the assistant (``你``/``您``), which never grants
# carry. Only these bounded openers participate; there is no list of individual
# acceptance sentences.
_SUBJECT_PRONOUN_PREFIXES = (
    "它", "它们", "他们", "她们", "他", "她",
    "这个", "那个", "这套", "那套", "这件", "那件", "这些", "那些",
    "这次", "那次", "此事", "这东西", "那东西",
    # deictic/adverbial openers: ``那样接入`` / ``这样会`` / ``那需要我`` point
    # at the *current* thread just as ``那个``/``这套`` do. A concrete new
    # subject in the remainder still overrides the carry downstream.
    "那样", "这样", "那般", "这般", "那么", "这么", "如此",
    "这", "那",
)

# Bounded, fixed frame vocabulary. Removing it from a normalised turn leaves the
# *concrete remainder*: a non-empty remainder means the turn carries a new
# subject and therefore never refers back to the current thread. The generic
# topical words mirror the consumer stopword policy so a status question about
# the thread (``效果有变好么``) reduces to nothing while a fresh subject
# (``你能解释海豚吗``) leaves ``海豚``.
_FRAME_PHRASES = (
    # address / request frames
    "问你个问题", "问你", "请问", "帮我", "帮忙", "给我", "告诉我", "告诉",
    "说说", "讲讲", "讲一下", "说一下", "聊一聊", "聊聊", "谈谈", "谈一谈",
    "解释一下", "解释", "介绍", "说明", "麻烦", "问题",
    # status / deixis / time phrases
    "到哪一步", "到哪", "做到", "一步", "进展", "变好", "变差", "怎么样",
    "怎么着", "怎么", "怎样", "如何", "为什么", "为何", "是否", "什么",
    "多少", "那个", "这个", "这些", "那些", "那次", "这次", "此次", "本次",
    "本轮", "上述", "前述", "前面", "刚才", "刚刚", "之前", "上面", "现在",
    "目前", "当前", "今天", "一开始", "开始", "还是", "才有",
    # generic topical words (consumer stopword policy)
    "方案", "计划", "项目", "情况", "状态", "内容", "效果", "交互", "准备",
    "需求", "设计", "开发", "测试", "工作", "流程", "功能", "方式", "方法",
    "结果", "总结", "文档", "系统", "数据", "信息", "时间", "用户", "卡片",
    "知识", "推送", "开头", "每轮", "最新", "相关", "进行", "继续", "一些",
    "一下", "对话", "动态",
    # generic cognition / status frames: verbs and role nouns that describe the
    # *thread* or the assistant, not a fresh subject (``打开``/``更新``/``依据``/
    # ``回答``/``质量``/``改变`` ...). Bounded and topic-free: a concrete noun in
    # the turn (``海豚``) still survives as the remainder.
    "打开", "资料", "更新", "依据", "找依据", "回答", "质量", "改变",
    "追问", "自行", "究竟", "每个", "助手",
    # generic self-action / evaluation predicates: they describe the thread or
    # the assistant's own behaviour, never a fresh concrete subject, so a turn
    # built only from them (``你用背景资料时会自行检索吗``) carries no subject.
    "确认", "检索", "背景", "有人", "预先", "起来", "顺畅", "顺手",
    # generic self-reference / lookup / subjective-appraisal predicates: an
    # ordinary reflexive question about the assistant's own behaviour or the
    # user's appraisal of the current thread (``你自己会查询背景资料吗`` /
    # ``你感觉回答质量有改变吗`` / ``您自己怎么看现在的效果``) is built only
    # from these plus the frames above, so it reduces to no concrete remainder
    # and refers back to the thread. A concrete noun in the remainder
    # (``你自己会查询海豚迁徙吗``) still survives as a fresh topic, so this
    # stays a small, topic-free class rather than a per-sentence allowlist.
    "自己", "感觉", "觉得", "查询", "看",
)

# Single frame characters: bounded function words plus a small measure-word set.
# A measure word belongs to the frame so ``几张`` (a pure quantity follow-up) has
# no concrete remainder, while ``海豚怎么定位`` keeps ``海豚定位``.
_FRAME_CHARS = set(
    "你您咱我们是有的了过会能可要想用得着给请帮做说会话问"
    "吧吗呢么嘛呀啊哦这那哪几多少啥"
    "张只条份次遍步段页行句篇块台套款种些场位名件颗粒头匹本支枝座辆艘架间层点"
    "在时一都它对更还下"
)

_FRAME_PHRASES_BY_LEN = tuple(sorted(_FRAME_PHRASES, key=len, reverse=True))


def _has_new_topic_marker(text):
    return any(marker in text for marker in _NEW_TOPIC_MARKERS)


def _is_question_shaped(text):
    return any(marker in text for marker in _QUESTION_MARKERS)


def _frame_residual(text):
    """The concrete remainder left after removing the bounded query frame.

    ``text`` is expected to be already normalised (lower-case, punctuation
    removed). An empty result is a generic frame with no new subject; a
    non-empty result carries a concrete remainder and so is not a referral.
    """
    value = text
    for phrase in _FRAME_PHRASES_BY_LEN:
        if phrase in value:
            value = value.replace(phrase, "")
    return "".join(ch for ch in value if ch not in _FRAME_CHARS)


def is_continuation(text):
    """Whether the turn explicitly asks to continue the *current* thread.

    A continuation marker must be the whole turn once the bounded frame is
    removed: ``继续``/``下一步做什么``/``然后呢`` continue the thread, but
    ``继续解释火山喷发`` introduces a fresh concrete subject and never carries
    the previous one. An explicit topic-switch phrase disables carry entirely.
    """
    stripped = normalise_query(discussion_text(text))
    if not stripped:
        return True
    if _has_new_topic_marker(stripped):
        return False
    matched = False
    for marker in sorted(_CONTINUATION_MARKERS, key=len, reverse=True):
        if marker in stripped:
            matched = True
            stripped = stripped.replace(marker, "")
    if not matched:
        return False
    return not _frame_residual(stripped)


def _starts_with_subject_pronoun(text):
    return any(text.startswith(cue) for cue in _SUBJECT_PRONOUN_PREFIXES)


def is_referential_followup(text):
    """Stable, bounded evidence that a turn is a referential follow-up.

    A follow-up points at the *current* thread instead of introducing a new
    concrete subject. Three bounded shapes qualify: an explicit deixis to the
    immediately preceding turns (``刚才``/``上述`` ...), a question that opens
    with a third-person/deictic subject pronoun (``它``/``这套``/``那个`` ...),
    or a *generic, question-shaped* query frame whose normalised text carries no
    concrete remainder once the bounded frame vocabulary is removed (``你现在是
    每轮推送吗`` / ``几张``). Addressing the assistant (``你``/``您``) or being
    short is never enough on its own, and an explicit topic-switch phrase
    disables carry even when a second-person address or a follow-up word is
    present.
    """
    stripped = normalise_query(discussion_text(text))
    if not stripped:
        return False
    if _has_new_topic_marker(stripped):
        return False
    if any(cue in stripped for cue in _DEIXIS_CUES):
        return True
    if _starts_with_subject_pronoun(stripped):
        return True
    if not _is_question_shaped(stripped):
        return False
    return not _frame_residual(stripped)


# A few function words are masked before CJK bigrams are cut.  Without this,
# "并发已经验证了吗" yields the cross-boundary bigrams "发已"/"经验"/"证了"/
# "了吗": "经验" then matches an unrelated card body and admits it as if the
# query were about experience.  Only query-side terms are masked; card terms
# and the shared lexical primitives remain untouched.
_QUERY_BOUNDARY_WORDS = ("已经",)
_QUERY_FINAL_PARTICLES = re.compile(r'[了吗呢吧啊]+(?=[\s，。？！,?!]|$)')


def _mask_query_function_words(text):
    for word in _QUERY_BOUNDARY_WORDS:
        text = text.replace(word, ' ')
    return _QUERY_FINAL_PARTICLES.sub(' ', text)


def specific_terms(text):
    """Query terms after removing bounded stopwords and crossing bigrams.

    The requested discussion is separated from inert output-format/schema/label
    spans first, so a schema field name can never become a consumer term. The
    bounded ordinary English function/action/format vocabulary is removed too,
    so an instruction-only English turn (``please explain compare``) has no
    specific terms and cannot summon a card that merely repeats those verbs.
    A small bounded function-word mask runs before terms() so a CJK bigram
    cannot straddle a particle/function-word boundary and become a false
    subject (``已经`` + ``验证`` -> ``经验``).
    """
    return terms(_mask_query_function_words(discussion_text(text))) - CONSUMER_GENERIC - _ORDINARY_ENGLISH


def card_terms(card):
    text = " ".join(str(card.get(k) or "") for k in ("key", "title", "topic", "body"))
    return terms(text)


def heading_terms(card):
    text = " ".join(str(card.get(k) or "") for k in ("key", "title", "topic"))
    return terms(text)


def _exact_identity_match(card, query):
    """Whether the query is (or contains / is contained in) a title/key.

    This is a raw identity helper only: callers decide whether a generic query
    may use it (own project) or must first carry a *specific* term (shared
    cards), and they pass a bounded ``identity`` string so a longer generic
    title can never be summoned through an unbounded substring.
    """
    q = normalise_query(query)
    if not q:
        return False
    for field in ("key", "title"):
        value = normalise_query(str(card.get(field) or ""))
        if not value:
            continue
        if q == value:
            return True
        # A longer question containing the exact title/key still counts, as
        # does a shorter query that is fully contained in the title/key.
        if len(value) >= 2 and value in q:
            return True
        if len(q) >= 2 and q in value:
            return True
    return False


def score_card(card, query_terms, specific):
    """Return a deterministic relevance dict for one card.

    ``query_terms`` is the set of *effective* terms (specific if the query has
    any, else the raw terms). ``specific`` is the specific-term set (possibly
    empty). No score ever depends on the corpus size.
    """
    cterms = card_terms(card)
    cheading = heading_terms(card)
    head_overlap = query_terms & cheading
    body_overlap = query_terms & cterms
    spec_overlap = specific & cterms
    score = 3 * len(head_overlap) + len(spec_overlap) + 0.5 * len(body_overlap - head_overlap)
    return {
        "score": score,
        "head_overlap": head_overlap,
        "body_overlap": body_overlap,
        "spec_overlap": spec_overlap,
    }


def admits(card, query, query_terms, specific, is_own, identity=None):
    """Whether a card has positive, non-generic relevance to the query.

    * own project: admitted on an exact title/key identity, a heading
      (key/title/topic) overlap, or a positive specific-term overlap. This is
      what lets a generic turn like ``方案`` surface the project's own card
      that actually carries that word.
    * shared: admitted *only* on a positive specific-term overlap. A lone
      generic word never admits a shared card -- not via a heading, not via a
      substring of its longer generic title (``招聘方案``), not via its body.
    * Project equality alone is not topical evidence: a raw/generic body
      overlap on its own never admits a card.

    ``identity`` is the bounded string used for exact-identity matching; it
    defaults to ``query``. Callers passing a bounded anchor string may pass an
    empty ``identity`` to disable identity matching entirely, so no unbounded
    whole-turn text can be smuggled in through the substring rank.
    """
    ident = query if identity is None else identity
    rel = score_card(card, query_terms, specific)
    if is_own:
        return bool(rel["spec_overlap"]) or bool(rel["head_overlap"]) \
            or _exact_identity_match(card, ident)
    return bool(rel["spec_overlap"])


def rank_consumer(cards, query, specific, is_own_lookup, identity=None):
    """Rank admitted cards deterministically.

    Ties break on updated_at then id, never on ``kind``: there is no
    memory-first ordering. A card with an exact identity match and/or heading
    overlap outranks one that merely shares body terms. ``identity`` bounds the
    exact-identity match (see :func:`admits`).
    """
    ident = query if identity is None else identity
    query_terms = specific if specific else terms(query)
    ranked = []
    for card in cards:
        own = is_own_lookup(card)
        if not admits(card, query, query_terms, specific, own, ident):
            continue
        rel = score_card(card, query_terms, specific)
        exact = 1 if _exact_identity_match(card, ident) else 0
        ranked.append((exact, rel["score"], card.get("updated_at", 0), card))
    ranked.sort(key=lambda t: (t[0], t[1], t[2], t[3].get("id") or ""), reverse=True)
    return [card for _e, _s, _u, card in ranked]


ANCHOR_MAX_TERMS = 24
ANCHOR_MAX_TURNS = 4


# ---------------------------------------------------------------------------
# Explicit named-subject handling (consumer path only).
#
# An English identifier or acronym (``FinchCore`` / ``MQTT`` / ``GPT4``) is a
# stable, concrete subject anchor: it outweighs generic action words
# (``产品化``/``升级``) that a *different* subject may also carry. It is matched
# against complete normalised word tokens, so ``ART`` never matches the ``art``
# inside ``party``. Only tokens that actually look like identifiers (a digit, an
# internal upper-case letter, or an all-caps acronym) are treated as subjects,
# so an ordinary lower-case English word is never mistaken for one.
# ---------------------------------------------------------------------------

_ASCII_RUN = re.compile(r'[A-Za-z0-9_]+')
_IDENTIFIER_FILLERS = {"ok", "okay", "hi", "hey", "oh", "pls", "plz", "thx"}


def _looks_like_identifier(token):
    if len(token) < 2 or not any(ch.isalpha() for ch in token):
        return False
    if any(ch.isdigit() for ch in token):
        return True
    if token.isupper():
        return True
    return any(ch.isupper() for ch in token[1:])


def _is_distinctive_identifier(token):
    """A named subject that an ordinary format/instruction word cannot mimic.

    A mixed-case token (``FinchCore``/``ZorBlax``) or one carrying a digit
    (``GPT4``/``Q3``) is unambiguously a proper name. An all-caps acronym is
    *not* by itself distinctive -- ``JSON``/``API`` appear in ordinary
    instructions -- so it only becomes an exclusive subject when a card
    actually names it.
    """
    if any(ch.isdigit() for ch in token):
        return True
    has_lower = any(ch.islower() for ch in token)
    upper_after_first = any(ch.isupper() for ch in token[1:])
    return has_lower and upper_after_first


def query_identifier_sets(query):
    """``(strict, acronyms)`` lower-cased identifier tokens of ``query``.

    ``strict`` are distinctive named subjects (mixed case or digit);
    ``acronyms`` are all-caps tokens, which are only treated as subjects when a
    card names them.
    """
    strict, acronyms = set(), set()
    for token in _ASCII_RUN.findall(query or ''):
        low = token.lower()
        if low in _IDENTIFIER_FILLERS or not _looks_like_identifier(token):
            continue
        (strict if _is_distinctive_identifier(token) else acronyms).add(low)
    return strict, acronyms


def query_identifiers(query):
    """Lower-cased identifier/acronym tokens of ``query`` as complete tokens."""
    strict, acronyms = query_identifier_sets(query)
    return strict | acronyms


def _card_words(card):
    """Complete lower-cased ASCII word tokens across a card's text.

    Matching against these tokens -- never a raw substring -- is what keeps
    ``art`` from matching inside ``party``.
    """
    text = ' '.join(str(card.get(field) or '')
                    for field in ('key', 'title', 'topic', 'body'))
    return {token.lower() for token in _ASCII_RUN.findall(text) if len(token) >= 2}


def _card_contains_phrase(card, phrases):
    """Whether a card's text carries one of the given contiguous phrases."""
    text = ' '.join(str(card.get(field) or '')
                    for field in ('key', 'title', 'topic', 'body'))
    return any(phrase and phrase in text for phrase in phrases or ())


# ---------------------------------------------------------------------------
# Case-insensitive subject resolution.
#
# A proper name may arrive in any case (``finchcore`` / ``FinchCore`` /
# ``FINCHCORE``). Distinctive mixed-case/digit identifiers and all-caps acronyms
# are recognised lexically above. A plain lower-case ASCII token is a subject
# only when it exactly equals a complete heading/key token of a candidate card
# (case-insensitively) and is not one of a small, bounded set of ordinary English
# function/action/format words -- so ``finchcore`` (which names a card) anchors
# while ``explain``/``json``/``the`` never do. Matching is on complete tokens,
# never substrings, so ``finchcore-next`` keeps the stable base token
# ``finchcore`` and ``next`` never matches inside another word.
# ---------------------------------------------------------------------------

_HEADING_FIELDS = ('key', 'title', 'topic')

# Bounded ordinary English function/action/format words that must never be read
# as a proper name even if a card happens to carry them as a heading token.
# Fixed and topic-free; nothing here is learned from the corpus or an answer.
_ORDINARY_ENGLISH = frozenset((
    'a', 'an', 'the', 'and', 'or', 'of', 'to', 'in', 'on', 'at', 'by', 'for',
    'with', 'from', 'about', 'as', 'is', 'are', 'was', 'were', 'be', 'been',
    'being', 'do', 'does', 'did', 'can', 'could', 'should', 'would', 'will',
    'shall', 'may', 'might', 'must', 'have', 'has', 'had', 'this', 'that',
    'these', 'those', 'it', 'its', 'we', 'you', 'they', 'he', 'she', 'i', 'me',
    'my', 'our', 'your', 'their', 'not', 'no', 'yes', 'ok', 'okay', 'please',
    'help', 'explain', 'describe', 'show', 'tell', 'give', 'list', 'return',
    'output', 'format', 'json', 'null', 'true', 'false', 'note', 'update',
    'check', 'verify', 'review', 'summary', 'plan', 'test', 'card', 'cards',
    'topic', 'title', 'body', 'answer', 'question', 'response', 'result',
    'data', 'system', 'user', 'time', 'new', 'old', 'next', 'last', 'current',
    'work', 'file', 'key', 'value', 'text', 'info', 'content', 'search',
    'query', 'find', 'get', 'set', 'add', 'use', 'using', 'make', 'made',
    # ordinary English action/instruction verbs and format/field words: these
    # describe *how* to answer, never *what* the turn is about, so they are
    # removed from the specific terms and never read as a proper name.
    'compare', 'evaluate', 'summarise', 'summarize', 'translate', 'rewrite',
    'generate', 'analyze', 'analyse', 'discuss', 'mention', 'respond', 'reply',
    'ask', 'define', 'clarify', 'compute', 'calculate', 'count', 'sort',
    'filter', 'merge', 'split', 'open', 'close', 'read', 'write', 'create',
    'delete', 'remove', 'look', 'fetch', 'retrieve', 'keep', 'maintain',
    'schedule', 'send', 'receive', 'api', 'http', 'https', 'url', 'uri',
    'csv', 'xml', 'html', 'yaml', 'toml', 'sql', 'pdf', 'txt', 'id', 'ids',
    'uuid', 'utf', 'ascii', 'regex', 'cache', 'config',
    # ordinary framing/instruction nouns: they bound *how* the answer should be
    # framed (a policy/rule discussion), never an actual named subject, so they
    # are removed from the specific terms and never truncate a known concrete
    # CJK subject (``policy 青沙馆布展怎么安排`` keeps 青沙馆).
    'policy', 'rule', 'rules', 'guideline', 'guidelines',
))


def _heading_ascii_tokens(cards):
    """Lower-cased complete ASCII tokens across every card heading/key."""
    tokens = set()
    for card in cards or ():
        text = ' '.join(str(card.get(field) or '') for field in _HEADING_FIELDS)
        for token in _ASCII_RUN.findall(text):
            low = token.lower()
            if len(low) >= 2 and any(ch.isalpha() for ch in low):
                tokens.add(low)
    return tokens


def subject_identifiers(query, cards):
    """Identifier tokens of ``query`` that actually identify a card subject.

    Returns the union of the distinctive strict identifiers/acronyms of the
    query (case-preserved evidence, lower-cased) with any lower-cased ASCII
    token that exactly equals a complete heading/key token of a candidate card
    and is not an ordinary English function/action/format word. This makes
    subject recognition case-insensitive for tokens that name a real card while
    never promoting an ordinary English word or a format request to a subject.
    """
    stripped = discussion_text(query)
    strict, acronyms = query_identifier_sets(stripped)
    found = set(strict) | set(acronyms)
    heading = _heading_ascii_tokens(cards)
    if not heading:
        return found
    for token in _ASCII_RUN.findall(stripped):
        low = token.lower()
        if low in found or low in _ORDINARY_ENGLISH:
            continue
        if len(low) < 2 or not any(ch.isalpha() for ch in low):
            continue
        if _is_distinctive_identifier(token):
            continue  # already carried as a strict identifier
        if low in heading:
            found.add(low)
    return found


def anchor_terms(turns, max_terms=ANCHOR_MAX_TERMS, max_turns=ANCHOR_MAX_TURNS):
    """Bounded specific terms drawn from recent substantive user turns.

    ``turns`` is an ordered list of ``(text, created_at, source_id)`` oldest
    first. Only turns with real specific terms participate; acknowledgements
    and generic-only turns are ignored. Explicit identifier evidence from up to
    ``max_turns`` recent turns is kept first, then the remaining bounded lexical
    terms fill the fixed ``max_terms`` bound, so a single long newest turn can
    never crowd the actual subject out with its first sorted CJK bigrams.
    """
    cap_terms = max(1, int(max_terms))
    cap_turns = max(1, int(max_turns))
    identifiers = []
    lexical = []
    used = 0
    for raw_text, _created, _sid in reversed(list(turns or ())):
        interp = interpret_request(raw_text)
        if (interp.ack_only or interp.no_history
                or is_acknowledgement(interp.text)):
            continue
        text = interp.text
        spec = specific_terms(text)
        if not spec:
            continue
        for term in sorted(query_identifiers(text) & spec):
            if term not in identifiers:
                identifiers.append(term)
        for term in sorted(spec):
            if term not in lexical:
                lexical.append(term)
        used += 1
        if used >= cap_turns:
            break
    collected = []
    for term in identifiers:
        if len(collected) >= cap_terms:
            break
        collected.append(term)
    for term in lexical:
        if len(collected) >= cap_terms:
            break
        if term not in collected:
            collected.append(term)
    return set(collected)


def _is_strong_topic(card, query, spec):
    """Exact identity, or a specific term landing in the card's heading."""
    if _exact_identity_match(card, query):
        return True
    return bool(spec & heading_terms(card))


def _same_subject_expansion(direct, strong, subject_terms, subject_phrases=None):
    """Keep every strong card plus full-body cards naming the same subject.

    ``direct`` is the ranked admitted set and ``subject_terms`` the terms that
    name the *same* subject (derived from query structure plus heading
    evidence, never from a fixture name). A card in ``direct`` whose text
    carries one of those terms (even only in its body) rides along, while an
    unrelated weak card cannot. When ``subject_phrases`` is given, a body card
    must carry the whole contiguous heading phrase instead of only a generic
    bigram of it, so a generic two-character overlap never rides along. Order
    follows the deterministic ``direct`` ranking; a strong card not otherwise
    admitted closes the list.
    """
    strong_ids = {card.get('id') for card in strong}
    chosen = []
    seen = set()
    for card in direct:
        cid = card.get('id')
        if cid in strong_ids:
            keep = True
        elif subject_phrases:
            keep = _card_contains_phrase(card, subject_phrases)
        elif subject_terms and subject_terms & card_terms(card):
            keep = True
        else:
            keep = False
        if keep and cid not in seen:
            seen.add(cid)
            chosen.append(card)
    for card in strong:
        cid = card.get('id')
        if cid not in seen:
            seen.add(cid)
            chosen.append(card)
    return chosen


def recover_subject_cards(cards, turns, is_own_lookup, on_recovered=None):
    """Recover the nearest concrete subject using the direct-query resolver.

    A loose heading bigram alone cannot replace the current thread's subject.
    ``on_recovered`` receives the actual positive anchor text, so callers can
    apply their final relevance gate without using a topic-free follow-up.
    An explicit unknown subject stops recovery rather than borrowing an older
    one. The supplied cards and bounded, oldest-first turns retain their scope.
    """
    for raw_text, _created, _sid in reversed(list(turns or ())):
        interp = interpret_request(raw_text)
        if (interp.ack_only or interp.no_history
                or is_acknowledgement(interp.text)):
            continue
        text = interp.text
        spec = specific_terms(text)
        if not spec:
            continue
        direct = rank_consumer(cards, text, spec, is_own_lookup, identity='')
        selected = _select_explicit_subject(cards, text, direct)
        if selected is not None:
            if selected and on_recovered is not None:
                on_recovered(text)
            return selected
    return []


# ---------------------------------------------------------------------------
# Cohesive subject resolution (consumer path only).
#
# One bounded routine answers "what concrete subject does this turn name?" for
# both scripts, from query structure plus card evidence -- never from a fixture
# name or a per-question exception:
#
# * a complete ASCII identifier declares a subject: a mixed-case/digit token, an
#   all-caps acronym, or a lower-case token that names a card heading. When a
#   card heading carries the token the subject is *known*; otherwise it is an
#   explicit but *unknown* subject that never falls back (``finchcore`` and
#   ``FINCHCORE`` and ``DuskForge`` are the same case-insensitive subject). An
#   ordinary English verb/function/format word is never a subject, and a token
#   glued directly to CJK (``JSON字段``) is an in-line format word, not a name;
# * a Chinese turn ``<subject>的<rest>`` wires the *leading* phrase, matched to
#   card headings by a contiguous common run, to the subject, so trailing
#   action words another card heading also carries (``南岭设施的低温灌溉``) never
#   enter. Several leading subjects joined by ``和``/``与``/space are all kept,
#   and a body-only card naming the same subject is added by bounded phrase
#   expansion downstream.
# ---------------------------------------------------------------------------

MIN_SUBJECT_PHRASE = 3
_CJK_RUN = re.compile(r'[\u3400-\u9fff]+')


def _is_cjk_char(ch):
    return '\u3400' <= ch <= '\u9fff'


def _card_heading_ascii_tokens(card):
    text = ' '.join(str(card.get(field) or '') for field in _HEADING_FIELDS)
    return {token.lower() for token in _ASCII_RUN.findall(text)
            if len(token) >= 2 and any(ch.isalpha() for ch in token)}


def _heading_cjk_runs(card):
    runs = []
    for field in _HEADING_FIELDS:
        runs.extend(_CJK_RUN.findall(str(card.get(field) or '')))
    return runs


def _longest_common_substring(a, b):
    """Longest contiguous common substring of two strings (exact).

    Exactly equivalent to the classic O(len(a)*len(b)) DP, including its
    tie-break: for the maximal length, the match whose end position in ``a`` is
    earliest (which, for a fixed length, is the earliest start in ``a``). The
    returned value is always a substring of ``a``.

    Implemented with Python's C-level string search over the substrings of the
    *shorter* string, so the per-character work happens in C instead of in a
    nested Python loop with a fresh list per row. This is an exact-match
    speedup, never an approximate similarity measure.
    """
    if not a or not b:
        return ''
    if len(a) <= len(b):
        # Enumerate ``a``'s substrings longest-first, left-to-right: the first
        # hit is the maximal length at its earliest start in ``a``.
        for length in range(len(a), 0, -1):
            for start in range(len(a) - length + 1):
                candidate = a[start:start + length]
                if candidate in b:
                    return candidate
        return ''
    # ``b`` is the shorter string: for each length take the substring of ``b``
    # that occurs earliest in ``a`` (same earliest-start tie-break).
    for length in range(len(b), 0, -1):
        best = -1
        for start in range(len(b) - length + 1):
            position = a.find(b[start:start + length])
            if position >= 0 and (best < 0 or position < best):
                best = position
                if best == 0:
                    break
        if best >= 0:
            return a[best:best + length]
    return ''


def _clear_ascii_token(query, start, end):
    """Whether the ASCII token stands as a complete, non-CJK-glued word."""
    before = query[start - 1] if start > 0 else ' '
    after = query[end] if end < len(query) else ' '
    return not (_is_cjk_char(before) or _is_cjk_char(after))


def _ascii_subject_classification(text, cards):
    """``(known, unknown)`` lower-cased ASCII subject tokens for one turn.

    The *same* bounded classification drives both compiled selection
    (:func:`resolve_query_subject`) and the pending-raw gate
    (:func:`raw_statement_subject_terms`), so the two paths can never disagree
    about whether an ASCII token names a known heading subject, an explicit
    unknown subject, or an ordinary instruction word. Classification never
    relies on lettercase alone: a lower-case token cleared of CJK on both
    sides is just as explicit an unknown as a mixed-case identifier.
    """
    heading_tokens = _heading_ascii_tokens(cards)
    known, unknown = set(), set()
    for match in _ASCII_RUN.finditer(text):
        token = match.group(0)
        low = token.lower()
        if len(low) < 2 or not any(ch.isalpha() for ch in low):
            continue
        if low in _ORDINARY_ENGLISH:
            continue
        if low in heading_tokens:
            known.add(low)
            continue
        if _is_distinctive_identifier(token) or _clear_ascii_token(text, match.start(), match.end()):
            unknown.add(low)
    return known, unknown


def resolve_query_subject(query, cards):
    """``(subject_terms, strong_cards, unknown_blank)`` for one consumer turn.

    ``subject_terms`` name the turn's concrete subject(s); ``strong_cards`` are
    the candidate cards whose *heading* names one of them; ``unknown_blank`` is
    true only when the turn names an explicit, uncorroborated subject and has no
    known one, so it must not fall back to an unrelated generic card. All three
    are empty/false for an ordinary generic turn.
    """
    cards = [card for card in (cards or ()) if isinstance(card, dict)]
    text = discussion_text(query)
    known, unknown = _ascii_subject_classification(text, cards)
    cjk_terms, strong_ids = set(), set()
    lead = text.split('\u7684', 1)[0] if '\u7684' in text else ''
    if lead:
        # Deduplicate query runs while keeping first-occurrence order
        # (``dict.fromkeys``). Identical runs/headings yield identical common
        # substrings and the update below is a strict ``>`` over first-
        # occurrence order, so dropping later duplicates cannot change the
        # winning string or its tie order. Heading runs are extracted once per
        # card instead of once per query run.
        lead_runs = list(dict.fromkeys(_CJK_RUN.findall(lead)))
        for card in cards:
            heading_runs = list(dict.fromkeys(_heading_cjk_runs(card)))
            best = ''
            for run in lead_runs:
                for heading_run in heading_runs:
                    common = _longest_common_substring(run, heading_run)
                    if len(common) > len(best):
                        best = common
            if len(best) >= MIN_SUBJECT_PHRASE:
                strong_ids.add(card.get('id'))
                cjk_terms |= {best[i:i + 2] for i in range(len(best) - 1)}
    strong, seen = [], set()
    for card in cards:
        cid = card.get('id')
        if cid in seen:
            continue
        if cid in strong_ids or (known and _card_heading_ascii_tokens(card) & known):
            strong.append(card)
            seen.add(cid)
    blank = bool(unknown) and not known and not strong_ids
    return set(known) | cjk_terms, strong, blank


def _cjk_heading_matches(scope, cards):
    """``(ids, terms, phrases)`` for cards naming a contiguous CJK phrase.

    For each card the longest common contiguous CJK run between ``scope`` and
    any heading field (key/title/topic) is taken; a run of at least
    ``MIN_SUBJECT_PHRASE`` characters that is not a bare deictic/frame phrase
    names that card's subject. ``terms`` are the run's bigrams (for the
    downstream same-subject gate) and ``phrases`` the whole run (for the whole-
    phrase body expansion).
    """
    # Deduplicate query runs and per-card heading runs while keeping first-
    # occurrence order (``dict.fromkeys``): identical pairs give identical
    # common substrings and the strict ``>`` update cannot change the winning
    # string or its tie order. Heading runs are extracted once per card rather
    # than once per query run.
    scope_runs = list(dict.fromkeys(_CJK_RUN.findall(scope)))
    if not scope_runs:
        return set(), set(), set()
    ids, terms, phrases = set(), set(), set()
    matches = []
    for card in cards:
        heading_runs = list(dict.fromkeys(_heading_cjk_runs(card)))
        best = ''
        for run in scope_runs:
            for heading_run in heading_runs:
                common = _longest_common_substring(run, heading_run)
                if len(common) > len(best):
                    best = common
        # A common suffix followed by a generic frame is not a subject:
        # "三季报数据" and "年报数据" share "报数据", but after stripping
        # the frame only the one-character suffix "报" remains. Require a
        # meaningful noun remainder rather than promoting that fragment.
        if len(best) >= MIN_SUBJECT_PHRASE and len(_frame_residual(best)) >= 2:
            # Trim trailing request/frame words only. Removing frame characters
            # inside a matched name would erase qualifiers ("一季报" -> "季报")
            # or turn "数据中心" into a different subject.
            subject = best
            while True:
                suffix = next((word for word in _FRAME_PHRASES_BY_LEN
                               if len(word) >= 2 and subject.endswith(word)), None)
                if suffix is None:
                    break
                subject = subject[:-len(suffix)]
            if len(subject) < 2:
                continue
            matches.append((card.get('id'), subject))
    for cid, subject in matches:
        # A suffix of an explicitly more specific noun is not a second topic.
        # Retain it if the query also names it independently (comparisons).
        longer = {other for _, other in matches if subject != other and subject in other}
        residual_scope = scope
        for other in sorted(longer, key=len, reverse=True):
            residual_scope = residual_scope.replace(other, ' ')
        if longer and subject not in residual_scope:
            continue
        ids.add(cid)
        phrases.add(subject)
        terms |= {subject[i:i + 2] for i in range(len(subject) - 1)}
    return ids, terms, phrases


def concrete_cjk_subject(text, cards, lead_only=False):
    """``(terms, strong_cards, phrases)`` for a concrete CJK heading subject.

    Uses bounded *contiguous* common runs between the concrete (non-frame) part
    of the current positive discussion and a card heading, so an explicit CJK
    subject is recognised even without ``的`` and beats a prior carry or a
    generic body overlap. A pure frame/deictic referral (``刚才那个怎么样``)
    reduces to no concrete remainder and names no subject, so a generic-title
    decoy never overrides the genuine earlier topic. The match is a whole
    heading-level phrase (``>= MIN_SUBJECT_PHRASE``), never a weak two-character
    word -- so a two-character generic overlap is never promoted.

    A genuine leading named subject (the phrase before the first ``的``) keeps
    focus, so a trailing generic action or another card name never rides along.
    When that leading phrase names *no* concrete subject because it is only a
    conversational frame (``我们回到前面的讨论，白榆测绘队...``), the positive
    substantive clauses after it are examined under the same contiguous-run and
    frame filter. This is a structural fallback, not a per-name rule.

    With ``lead_only`` only the leading (pre-``的``) structure is examined: an
    explicit *unknown* named subject keeps its never-fall-back blank unless a
    genuine known CJK subject stands in the same leading structure -- a generic
    suffix after it (``drifthollow 的升级方案``) is never promoted to the
    subject, in any letter case, while a known subject named without ``的``
    (``policy 青沙馆布展怎么安排``) still wins over a stray ordinary word.
    """
    cards = [card for card in (cards or ()) if isinstance(card, dict)]
    base = discussion_text(text)
    stripped = normalise_query(base)
    if not stripped or not _frame_residual(stripped):
        return set(), [], set()
    scopes = base.split('\u7684', 1) if '\u7684' in base else [base]
    if lead_only:
        scopes = scopes[:1]
        # An explicit *unknown* named subject ends the leading structure: a
        # generic action phrase standing after it (``drifthollow 升级方案``),
        # with or without ``的``, is never promoted into a concrete CJK
        # subject. The unknown set comes from the *same* shared ASCII subject
        # classification as every other gate, so the decision never relies on
        # lettercase. A genuine known CJK subject named *before* the unknown
        # token still wins; an ordinary English instruction word is not an
        # unknown and never truncates (``policy 青沙馆布展怎么安排``).
        unknown = _ascii_subject_classification(base, cards)[1]
        if unknown:
            cut = min((m.start() for m in _ASCII_RUN.finditer(scopes[0])
                       if m.group(0).lower() in unknown), default=None)
            if cut is not None:
                scopes = [scopes[0][:cut]]
    for scope in scopes:
        ids, terms, phrases = _cjk_heading_matches(scope, cards)
        if ids:
            strong = [card for card in cards if card.get('id') in ids]
            return terms, strong, phrases
    return set(), [], set()


def raw_statement_subject_terms(query, cards, turns=()):
    """Subject terms a *related* raw statement must name, or ``set()``.

    The raw-statement fallback uses this so it passes the *same* subject gate as
    compiled selection. When the current turn names no subject, the nearest
    substantive same-session turn is consulted, so a follow-up keeps the
    thread's subject. An empty result means the turn carries no subject
    evidence and the caller keeps its ordinary specific-term gate. Every turn
    is read through :func:`interpret_request`, so a negated clause never supplies
    a subject term.
    """
    interp = interpret_request(query)
    text = interp.text
    terms_, _cards, blank = resolve_query_subject(text, cards)
    if terms_:
        return terms_
    if blank:
        # The *same* unknown-subject decision as compiled selection: an
        # explicit unknown named subject constrains a pending raw statement to
        # that actual complete ASCII token (matched case-insensitively through
        # the tokenizer), so an unrelated pending statement that only shares
        # generic action words (``木栈系统的升级方案尚待审批``) never enters and
        # no old session anchor is borrowed. This is not a blanket raw
        # suppression: a pending statement that actually names the subject
        # (``DriftHollow 的部署尚未定稿``) still names the token and is
        # offered. A generic action phrase (``升级方案``), with or without
        # ``的``, is never promoted into a concrete subject here either.
        _known, unknown = _ascii_subject_classification(
            discussion_text(text), [c for c in (cards or ()) if isinstance(c, dict)])
        return set(unknown)
    cjk_terms, _cjk_cards, _cjk_phrases = concrete_cjk_subject(text, cards)
    if cjk_terms:
        return cjk_terms
    for turn_text, _created, _sid in reversed(list(turns or ())):
        turn_interp = interpret_request(turn_text)
        if (turn_interp.ack_only or turn_interp.no_history
                or is_acknowledgement(turn_interp.text)):
            continue
        turn_terms, _c, _b = resolve_query_subject(turn_interp.text, cards)
        if turn_terms:
            return turn_terms
        # Same concrete-CJK fallback as the current turn: a prior turn whose
        # pre-的 phrase is only a conversational frame still names its subject
        # through the substantive clause after it.
        turn_cjk, _cc, _cp = concrete_cjk_subject(turn_interp.text, cards)
        if turn_cjk:
            return turn_cjk
    return set()


def _has_distinctive_unknown_subject(query, cards):
    """Whether the turn names an explicit *distinctive* unknown subject.

    Mirrors the unknown classification of :func:`resolve_query_subject` for
    mixed-case/digit identifiers only (``ZorBlax``/``DuskForge``): those keep
    their exclusive never-fall-back blank even when the same turn also matches
    a known CJK heading. An ordinary lower-case ASCII instruction word
    (``policy``) is not distinctive, so it never discards a known concrete CJK
    subject found elsewhere in the turn.
    """
    text = discussion_text(query)
    heading_tokens = _heading_ascii_tokens(cards)
    for match in _ASCII_RUN.finditer(text):
        token = match.group(0)
        low = token.lower()
        if (len(low) >= 2 and any(ch.isalpha() for ch in low)
                and low not in _ORDINARY_ENGLISH and low not in heading_tokens
                and _is_distinctive_identifier(token)):
            return True
    return False


def _select_explicit_subject(all_cards, query, direct):
    """Share concrete-subject admission between current and prior turns.

    None means no concrete subject; [] means an explicit unknown. Whole CJK
    phrases constrain body-only supplements on both selection paths.
    """
    subject_terms, subject_cards, unknown_blank = resolve_query_subject(query, all_cards)
    if subject_cards:
        return _same_subject_expansion(direct, subject_cards, subject_terms)
    cjk_terms, cjk_cards, cjk_phrases = concrete_cjk_subject(
        query, all_cards, lead_only=bool(unknown_blank))
    if cjk_cards and not (unknown_blank
                          and _has_distinctive_unknown_subject(query, all_cards)):
        return _same_subject_expansion(direct, cjk_cards, cjk_terms,
                                       subject_phrases=cjk_phrases)
    if unknown_blank:
        return []
    # A complete short title/key is an explicit name too (e.g. 封签).
    # Preserve it without promoting an incidental two-character overlap in a
    # longer conversational frame into a subject.
    identity = normalise_query(query)
    exact = [card for card in all_cards if identity and any(
        identity == normalise_query(str(card.get(field) or ''))
        for field in ('title', 'key'))]
    if exact and specific_terms(query):
        return _same_subject_expansion(direct, exact, specific_terms(query),
                                       subject_phrases={identity})
    return None


def select_consumer_topic(all_cards, query, project, prior_provider=None,
                          on_recovered=None):
    """Choose the cards one consumer turn should inject (deterministic).

    ``all_cards`` is the authorized candidate set (own project plus read-only
    shared). ``prior_provider`` is a lazy callable returning the bounded recent
    same-session user turns ``(text, created_at, source_id)``; it is consulted
    only for a continuation/referral, never for a fresh turn.

    Precedence, all bounded and corpus-size independent:

    * acknowledgement -> nothing;
    * a concrete current subject -> only the cards whose heading names that
      subject (or *any* of several named subjects) plus same-subject body
      cards. This holds in any letter case. An explicit but *unknown*
      ASCII-named subject never falls back to an unrelated generic card; a
      concrete known CJK subject found elsewhere in the same turn still wins
      over a stray ordinary ASCII instruction word (while an explicit
      *distinctive* unknown identifier still blanks), and a turn carrying
      *only* unknown subjects stays empty. A concrete subject outranks a
      referral carry, so a concrete new subject still wins inside a deictic
      sentence;
    * a genuine continuation or referential follow-up -> the recovered
      same-session subject, decided *before* a generic title overlap can be
      taken for a fresh topic. Recovery walks the bounded turns newest first and
      takes the nearest identifiable subject under the *same* subject constraint
      as direct retrieval, so a long generic turn cannot crowd out the genuine
      earlier topic and a closer different subject overrides an older one; when
      no turn names a usable subject, a small own project-state set is used;
    * an ordinary direct turn -> a strong heading match plus full-body cards
      that name that *same* subject, so a weak card cannot ride along on one
      shared generic body term;
    * otherwise -> the positively admitted direct matches (possibly empty).

    The requested *discussion* is separated from inert output-format/schema/
    label spans once, up front, and that same text drives every lexical step
    (term extraction, subject detection, continuation detection) and the
    earlier-turn recovery, so a schema field name can never be read as a
    subject on one path while being gated on another.
    """
    if is_acknowledgement(query):
        return []
    interp = interpret_request(query)
    if interp.ack_only or interp.no_history:
        return []
    q = interp.text
    spec = specific_terms(q)
    is_own = lambda card: card.get('project') == project
    direct = rank_consumer(all_cards, q, spec, is_own)

    explicit = _select_explicit_subject(all_cards, q, direct)
    if explicit is not None:
        return explicit

    followup = is_continuation(q) or is_referential_followup(q)
    if followup and prior_provider is not None:
        recovered = recover_subject_cards(all_cards, prior_provider() or [], is_own,
                                          on_recovered=on_recovered)
        if recovered:
            return recovered

    strong = [card for card in direct if _is_strong_topic(card, q, spec)]
    if strong:
        strong_terms = set()
        for card in strong:
            strong_terms |= spec & heading_terms(card)
        return _same_subject_expansion(direct, strong, strong_terms)

    if followup:
        status = [card for card in all_cards
                  if card.get('project') == project and card.get('kind') == 'memory']
        status.sort(key=lambda card: (card.get('updated_at', 0), card.get('id') or ''),
                    reverse=True)
        return status[:4]

    return direct
