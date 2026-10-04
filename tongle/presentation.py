"""Readable Markdown presentation; identity remains stable in the database."""
import json,re

_WINDOWS_RESERVED={'CON','PRN','AUX','NUL'}|{'COM'+n for n in '123456789¹²³'}|{'LPT'+n for n in '123456789¹²³'}

def windows_reserved_name(name):
    if not isinstance(name,str):return False
    stem=name.split('.',1)[0].rstrip(' .').upper()
    return stem in _WINDOWS_RESERVED

def label(text,fallback='未命名记录',limit=36):
    value=re.sub(r'[\x00-\x1f<>:"/\\|?*#\[\]`%]',' ',str(text))
    value=re.sub(r'\s+',' ',value).strip(' .')[:limit].strip(' .')
    value=value or fallback
    if windows_reserved_name(value):
        value='_'+value
    return value

def folder(value):
    if not isinstance(value,str) or not value or value in ('.','..') or '/' in value or '\\' in value or value.startswith('.') or len(value)>80:
        raise ValueError('invalid managed folder')
    return value

def title(rec):
    if rec.get('note_title'):return label(rec['note_title'])
    text=re.split(r'[。！？\n]',rec['text'].strip())[0]
    return label(text)

def hidden_metadata(rec):
    return '%%\ntongle-meta: '+json.dumps({k:rec[k] for k in ['id','project','source_id','source_version']},ensure_ascii=False)+'\n%%\n\n'
