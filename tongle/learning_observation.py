"""Conservative local linking of explicit follow-up corrections; no model call.

A matching phrase is only a discovery signal. The learning reviewer must still
check whether the captured statement concerns the earlier task. It never marks
business success or copies the user's text into an invented user event.
"""
import re

_CORRECTION = re.compile(r"(你.{0,6}(搞错|理解错|弄错)|刚才.{0,12}(不对|错误|失败|没有)|实际.{0,12}(失败|报错|不符合)|验收.{0,8}(失败|未通过)|测试.{0,8}(失败|报错)|纠正[：:，,]|不是.{0,12}而是)")


def capture_followup(evolution, source_id, session):
    from .evolution import config
    cfg=config(evolution.engine)
    projects=cfg.get('knowledge_learning_projects')
    if not isinstance(projects,list) or not projects:
        return None
    source=evolution.engine.store.source(source_id)
    if source['role']!='user' or source['project'] not in projects:
        return None
    if source['session']!=session or not _CORRECTION.search(source['text'][:4000]):
        return None
    receipt=evolution.db.execute(
        "SELECT c.id FROM knowledge_consumption c JOIN sources s ON s.id=c.output_source "
        "WHERE c.project=? AND c.session=? AND c.binding='receipt' "
        "AND s.created_at<? ORDER BY s.created_at DESC,c.created_at DESC LIMIT 1",
        (source['project'],session,source['created_at'])).fetchone()
    if not receipt:
        return None
    from .learning import Learning
    return Learning(evolution).observe(source['project'],receipt['id'],source_id)
