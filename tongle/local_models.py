"""Optional offline ONNX inference. No downloads, credentials, or daemon here."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path


class ModelUnavailable(ValueError):
    pass


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def input_length(cfg):
    length = cfg.get('max_length', 384)
    if type(length) is not int or not 8 <= length <= 512:
        raise ModelUnavailable('local model max_length must be between 8 and 512 tokens')
    return length


def model_key(config):
    if not isinstance(config,dict) or not config.get('path') or not config.get('revision'):
        raise ModelUnavailable('local model needs a path and frozen revision')
    return hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()


@lru_cache(maxsize=3)
def _load(serialized):
    cfg = json.loads(serialized)
    length = input_length(cfg)
    root = Path(cfg['path']).expanduser()
    model = root / cfg.get('onnx','model.onnx')
    tokenizer = root / 'tokenizer.json'
    if not model.is_file() or not tokenizer.is_file():
        raise ModelUnavailable('local model files are missing; bind/download before activation')
    for path,field in ((model,'sha256'),(tokenizer,'tokenizer_sha256')):
        if cfg.get(field) and file_sha256(path)!=cfg[field]:
            raise ModelUnavailable('local model integrity mismatch')
    try:
        import onnxruntime as ort
        from tokenizers import Tokenizer
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(str(model),sess_options=options,providers=['CPUExecutionProvider'])
        tok = Tokenizer.from_file(str(tokenizer))
        tok.enable_truncation(max_length=length)
        tok.enable_padding(pad_id=cfg.get('pad_id',0))
        return session,tok
    except Exception as exc:
        raise ModelUnavailable('local inference dependency or model unavailable') from exc


def _run(cfg, texts):
    import numpy as np
    model_key(cfg)
    session,tok = _load(json.dumps(cfg,sort_keys=True))
    encoded = tok.encode_batch(texts)
    arrays = {'input_ids':np.asarray([e.ids for e in encoded],dtype=np.int64),
              'attention_mask':np.asarray([e.attention_mask for e in encoded],dtype=np.int64),
              'token_type_ids':np.asarray([e.type_ids for e in encoded],dtype=np.int64)}
    try:
        output = session.run(None,{i.name:arrays[i.name] for i in session.get_inputs()})[0]
    except Exception as exc:
        raise ModelUnavailable('local model inference failed') from exc
    return output,arrays['attention_mask']


def embed(cfg, texts, query=False):
    import numpy as np
    prefix=cfg.get('query_prefix' if query else 'document_prefix','')
    result=[]
    for start in range(0,len(texts),16):
        output,mask=_run(cfg,[prefix+t for t in texts[start:start+16]])
        if output.ndim==2:
            vectors=output
        elif cfg.get('pooling','mean')=='cls':
            vectors=output[:,0]
        else:
            vectors=(output*mask[:,:,None]).sum(axis=1)/np.maximum(mask.sum(axis=1)[:,None],1)
        vectors=vectors/np.maximum(np.linalg.norm(vectors,axis=1,keepdims=True),1e-12)
        result.extend(vectors.astype('float32'))
    return np.asarray(result,dtype='float32')


def rerank(cfg, query, texts):
    import numpy as np
    result=[]
    for start in range(0,len(texts),8):
        logits,_=_run(cfg,[(query,t) for t in texts[start:start+8]])
        result.extend(np.asarray(logits).reshape(-1).tolist())
    return result
