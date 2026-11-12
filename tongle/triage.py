"""Failure classification shared by the doctor and the triage tool.

The classification is deliberately deterministic and conservative: it only
decides which *next action* is safe. It never re-runs work, rewrites a card,
or treats a model rejection as a transient infrastructure error.
"""

TRANSIENT = 'transient_provider'
CONTENT = 'content'
CAPACITY = 'capacity'
UNKNOWN = 'unknown'

ACTIONS = {
    TRANSIENT: '确认上游恢复后重排一次；仍失败则隔离，不反复重试',
    CONTENT: '保留为待核，不盲目连续重试；超大任务需拆分/缩小输入后重排一次',
    CAPACITY: '裁剪载荷或关闭自动依赖维护；不要把超限当内容失败',
    UNKNOWN: '查看 knowledge_failures 审计后人工定性',
}


def classify(reason):
    text = (reason or '').lower()
    if 'request_limit' in text:
        # A local per-run request cap, not an upstream 429. Requeueing the same
        # oversized job only spends calls until it hits the same wall.
        return CONTENT
    if any(x in text for x in ('provider_failure', 'timeout',
                               'connection', 'rate limit', 'upstream')):
        return TRANSIENT
    if 'capacity exceeded' in text:
        return CAPACITY
    if any(x in text for x in ('needs review', 'semantic review not approved',
                               'stage limit', 'stagelimit',
                               'selectionchanged', 'segment may be replaced',
                               'checks must be exactly one',
                               'review not approved',
                               'evidence version', 'basis in changing batch')):
        return CONTENT
    return UNKNOWN
