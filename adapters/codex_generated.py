"""Exact known Codex application protocol, not a keyword classifier.

Keep the complete observed template so quoted/edited user text is unaffected.
Suppressed native turns retain a bounded hash audit in adapter state. Unknown
formats are captured normally, never guessed from prefixes or JSON responses.
"""
import hashlib

SUGGESTION_TEMPLATE = 'This is an app-generated request for a suggested next user message. The user did not write this message.\n\nYour suggestion will appear in the user\'s empty message composer. They can accept it, edit it, or ignore it; it will not be sent automatically.\n\nPredict the single message this user is most likely to type next from the visible conversation. Use the whole conversation to understand their current intent and the latest exchange. Choose the action and specific object that best continue that intent, rather than a generally useful follow-up. Express its core naturally in the user\'s voice, keeping the requirements needed to retain its meaning. Choose the strongest grounded move even if several continuations are possible; uncertainty about exact wording or incidental details is not a reason to return null. Use null only when the conversation offers no grounded next move at all.\n\nSet suggestion to exactly one natural user message, one line, at most 240 characters, or null. No alternatives or explanation.\n\nReturn only JSON matching this schema: {"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","properties":{"suggestion":{"anyOf":[{"type":"string"},{"type":"null"}]}},"required":["suggestion"],"additionalProperties":false}.'

def turn_key(session_id, turn):
    return hashlib.sha256(('codex\0'+session_id+'\0'+turn).encode()).hexdigest()

def suppress_prompt(state, host, session_id, payload, prompt):
    turn=payload.get('turn_id')
    if host != 'codex' or not isinstance(turn,str) or not turn or prompt != SUGGESTION_TEMPLATE:
        return False
    turns=state.setdefault('app_generated_turns',{})
    turns[turn_key(session_id,turn)]={'reason':'codex_suggestion_template_v1','prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest()}
    while len(turns)>1000:
        del turns[next(iter(turns))]
    return True

def suppress_stop(state, host, session_id, payload):
    turn=payload.get('turn_id')
    return host=='codex' and isinstance(turn,str) and bool(turn) and turn_key(session_id,turn) in state.get('app_generated_turns',{})
