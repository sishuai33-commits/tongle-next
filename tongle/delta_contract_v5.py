"""V5 clarifies responsibility and no-op review; V1-V4 remain frozen.

Validation remains strict. Only rejection diagnostics and model instructions
change; no automatic conversion of rejected output into accepted content.
"""
from . import delta_contract as _base
from . import delta_contract_v4 as _v4

_PLAN_APPEND = (
    '责任边界示例：若 owned_source_refs=[s11,s12]、reference_source_refs=[s1,s2]，'
    'dispositions 必须且只能处理 s11,s12；不得为 s1,s2 新建 disposition，'
    '也不得仅为补记 reference 的旧报告新增 change。参考片段只是理解本次 owned 的上下文。'
    'disposition.source_refs 与 change.source_refs 的允许范围不同：前者只能 owned，'
    '后者可引用理解并支持本次 owned 变更所必需的 reference。'
)
_REVIEW_APPEND = (
    '按每条 disposition.decision 实值分别审核，不要把 nonmaterial 改读为 covered。'
    'nonmaterial 表示来源没有需长期保存的语义增量：只核实确实无重要事实/决定/职责遗漏；'
    '它不要求 card_refs，不要求已有卡片，changes=[]、final_cards=[] 可以完全正确。'
    '仅 decision=covered 才要求已存在 active 卡片覆盖证据。'
    '示例：纯应用输出格式指令被标为 nonmaterial 且无变更，不得仅因没有覆盖卡判 fail；'
    '但真实用户决定被错丢为 nonmaterial 仍必须 fail。'
)

def plan_payload(ctx):
    payload = _v4.plan_payload(ctx)
    payload['task'] += _PLAN_APPEND
    return payload


def review_payload(ctx, plan):
    payload = _v4.review_payload(ctx, plan)
    payload['task'] += _REVIEW_APPEND
    return payload


def compile_plan(ctx, response):
    ctx = _base.revalidate_context(ctx)
    allowed = _base._owned_span_refs(ctx)
    dispositions = response.get('dispositions') if isinstance(response, dict) else None
    if isinstance(dispositions, list):
        for i, item in enumerate(dispositions):
            refs = item.get('source_refs') if isinstance(item, dict) else None
            if isinstance(refs, list) and all(isinstance(x, str) for x in refs):
                bad = [x for x in refs if x not in allowed]
                if bad:
                    # Bound diagnostic size; never echo arbitrary model text.
                    aliases = [x for x in bad if x.startswith('s') and x[1:].isdigit() and len(x)<12]
                    raise ValueError('disposition %d contains non-owned source refs %s; allowed owned refs %s; remove reference-only dispositions, do not widen ownership' % (i, aliases[:16], allowed[:64]))
    return _v4.compile_plan(ctx, response)


def __getattr__(name):
    return getattr(_v4, name)
