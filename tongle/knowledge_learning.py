"""Read-time validation of version-bound correction adoption. No new storage."""
import hashlib
import json
import sqlite3

from .source_memory import SourceMemory, _VerificationUnknown
from .retrieval_budget import verify_identity


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class _CorrectionReader(SourceMemory):
    """Reuse current correction rules without the writer's schema initializer."""
    def __init__(self,engine):
        self.engine,self.db=engine,engine.db


def adoption_record(card,correction):
    return {'project':card['project'],'card_id':card['id'],'card_version':card['version'],
            'body_hash':_digest(card['body']),'event_key':correction['event_key'],
            'source_id':correction['source_id'],'source_version':correction['source_version'],
            'quote':correction['quote'],'replacement':correction['replacement']}


def correction_adopted(engine,card,correction):
    """True only for this current card's complete, still-valid adoption.

    Callers supply a full current card, not an excerpt or a descendant. The
    persisted correction remains effective; this only permits its revised body.
    No caller-provided ``adopted`` or ``effective`` flag is trusted on its own.
    """
    try:
        if not isinstance(card,dict) or not isinstance(correction,dict):return False
        if correction.get('target_card_id')!=card['id']:return False
        current=engine.db.execute('SELECT * FROM knowledge_cards WHERE id=?',(card['id'],)).fetchone()
        if current and not verify_identity(engine,'card',card['id'],current['version']):return False
        if not current or current['state']!='active' or current['manually_edited']:return False
        if any(current[key]!=card[key] for key in ('project','version','body')):return False
        row=engine.db.execute('SELECT payload FROM inline_note_meta WHERE card_id=? AND version=? AND project=?',
                              (card['id'],card['version'],card['project'])).fetchone()
        if not row:return False
        meta=json.loads(row[0])
        if not isinstance(meta,dict):return False
        if (meta.get('authority'),meta.get('status'))!=('assistant_inference','hypothesis'):return False
        reason=meta.get('revision_reason')
        if not isinstance(reason,str) or not reason.strip() or len(reason)>800:return False
        if meta.get('body_hash')!=_digest(card['body']):return False
        count=meta.get('statement_chars')
        if type(count) is not int or not 1<=count<=6000:return False
        statement=card['body'][:count]
        if _digest(statement)!=meta.get('statement_hash'):return False
        corrections=_CorrectionReader(engine).corrections(card['project'],False,target_ids={card['id']})
        if any(item.get('verification_unknown') for item in corrections):return False
        live=[item for item in corrections
              if item['effective'] and item['target_card_id']==card['id']]
        if not live:return False
        matching=[item for item in live if item['event_key']==correction.get('event_key')]
        if not matching:return False
        fields=('project','target_card_id','target_card_version','source_id','source_version','quote','replacement')
        if any(matching[0][key]!=correction.get(key) for key in fields):return False
        records=meta.get('adopted_corrections')
        if not isinstance(records,list) or len(records)!=len(live):return False
        expected=[adoption_record(card,item) for item in live]
        if any(record not in records for record in expected):return False
        evidence=json.loads(current['evidence'])
        reader=_CorrectionReader(engine)
        for ref in evidence:
            source=reader._source(ref['source_id'],ref['version'])
            if (source['project']!=card['project'] or source['current_version']!=ref['version']
                    or not engine.source_available(ref['source_id']) or ref['quote'] not in source['text']):
                return False
        for item in live:
            exact={'source_id':item['source_id'],'version':item['source_version'],'quote':item['quote']}
            if exact not in evidence or item['replacement'] not in statement:return False
        return True
    except (_VerificationUnknown,KeyError,ValueError,TypeError,sqlite3.Error):
        return False


def _own_adoptions_valid(engine,card):
    try:
        if not isinstance(card,dict):return False
        if not engine.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inline_note_meta'").fetchone():
            return True
        row=engine.db.execute('SELECT payload FROM inline_note_meta WHERE card_id=? AND version=? AND project=?',
                              (card['id'],card['version'],card['project'])).fetchone()
        if not row:return True
        meta=json.loads(row[0])
        if not isinstance(meta,dict):return False
        records=meta.get('adopted_corrections',[])
        if not isinstance(records,list):return False
        if not records:return True
        corrections=_CorrectionReader(engine).corrections(card['project'],False,target_ids={card['id']})
        if any(item.get('verification_unknown') for item in corrections):return False
        live={item['event_key']:item for item in corrections
              if item['effective'] and item['target_card_id']==card['id']}
        return all(record['event_key'] in live
                   and correction_adopted(engine,card,live[record['event_key']]) for record in records)
    except (KeyError,ValueError,TypeError,sqlite3.Error):
        return False


def adopted_corrections_valid(engine,card):
    """Validate own and existing basis adoptions, read-only and bounded.

    A pending ancestor correction cannot revive a derived conclusion. Follow
    only persisted dependencies (at most 128 distinct cards, depth 32); never
    infer links or schedule maintenance when a dependency is unavailable.
    """
    try:
        if not isinstance(card,dict):return False
        todo=[(card,frozenset())];checked=set()
        while todo:
            current,path=todo.pop();cid=current['id']
            if not verify_identity(engine,'card',cid,current['version']):return False
            if cid in path or len(path)>32:return False
            if cid in checked:continue
            if len(checked)>=128 or not _own_adoptions_valid(engine,current):return False
            checked.add(cid)
            if not engine.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_dependencies'").fetchone():
                continue
            refs=engine.db.execute('SELECT basis_id,basis_version FROM knowledge_dependencies WHERE card_id=?',(cid,)).fetchall()
            for ref in refs:
                row=engine.db.execute('SELECT * FROM knowledge_cards WHERE id=?',(ref['basis_id'],)).fetchone()
                if row and not verify_identity(engine,'card',ref['basis_id'],row['version']):return False
                if (not row or row['state']!='active' or row['version']!=ref['basis_version']
                        or row['project'] not in (current['project'],'shared')):
                    return False
                todo.append((dict(row),path|{cid}))
        return True
    except (KeyError,ValueError,TypeError,sqlite3.Error):
        return False
