"""Shared bounded model-failure protocol. No raw provider text is retained.

Every model boundary (subprocess runner, worker, evolution) reports failures
through a small whitelist so callers and the local ledger never store prompts,
provider headers, secrets, or arbitrary exception strings. A failure carries
only a coarse category, a small safe code, an optional safe stop reason, an
optional HTTP status, and whatever *known* usage was observed before the
failure.

Usage unknown is never silently treated as zero: known fields stay real
integers, unknown fields are dropped (None), and a partial dict remains
partial. Downstream aggregation must preserve that distinction.
"""

# Coarse, stable categories. These are the only values callers should switch
# on; they classify provider failure/timeout/oversize/truncation/empty output
# and malformed JSON.
CATEGORIES = (
    'provider_failure',   # process/provider exited nonzero or non-timeout error
    'timeout',            # model call exceeded its deadline
    'input_limited',      # prompt/input byte budget exceeded (local guard)
    'output_truncated',   # provider stopped because output cap was reached
    'empty_output',       # final assistant text was empty
    'invalid_json',       # output was not a JSON object
)

# Safe, bounded machine codes. Nothing free-form: unknown provider text is
# reduced to 'provider_error', never stored verbatim.
SAFE_CODES = (
    'provider_error', 'network_error', 'rate_limited', 'authentication_error', 'timeout', 'output_too_large', 'input_too_large',
    'truncated', 'empty_output', 'invalid_json', 'invalid_json_type',
    'missing_object', 'missing_records', 'too_many_records',
    # Local per-worker provider-request count guard (exit 8 in model_budget.ts).
    # Reported as a provider_failure so the parent preserves the bounded cause
    # instead of collapsing it to a generic provider_error.
    'request_limit',
    # A bounded, whitelisted *prior* error label may accompany request_limit
    # so the ledger can keep the guard cause while noting the previous failure.
    'prior_rate_limited', 'prior_network_error', 'prior_authentication_error',
    'prior_provider_error',
    # The runner exited without emitting its bounded protocol line, so nothing
    # is actually known about what the provider said (or whether it was reached).
    # Kept distinct from provider_error on purpose: "the model provider failed"
    # and "our runner process died first" need different responses, and the
    # undifferentiated label already caused one misdiagnosed outage.
    'runner_crash',
)

# Only these stop reasons are ever echoed back. Anything else collapses to None
# so a provider cannot smuggle text through stopReason.
SAFE_STOP_REASONS = ('stop', 'length', 'max_tokens', 'endTurn', 'content_filter', 'error', 'aborted', 'toolUse')

_UNKNOWN_STOP = 'unknown'


def safe_code(value):
    """Return a whitelisted code or a constant fallback; never raw text."""
    return value if value in SAFE_CODES else 'provider_error'


def safe_stop_reason(value):
    """Return a whitelisted stop reason or None; never a free-form string."""
    if isinstance(value, str) and value in SAFE_STOP_REASONS:
        return value
    return None


def safe_status(value):
    """Return an integer HTTP status in 100..599, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    return None


def known(value):
    """True only for a real, already-observed non-negative integer token count.

    A missing, None, bool, or negative value is *unknown*, never zero.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def normalize_usage(raw):
    """Bound an observed usage dict to known non-negative integers.

    Unknown/partial fields are omitted rather than defaulted to zero. Returns a
    dict that may be empty; callers must keep the 'some fields unknown' meaning.

    ``inputIncludesCache`` is preserved as an explicit boolean marker only when
    the raw value is literally ``True`` *and* ``input`` is a known non-negative
    integer. Falsy/truthy non-boolean lookalikes (``False``, ``'true'``, ``1``)
    do not activate the marker, so a provider cannot smuggle a strength claim
    through type coercion.
    """
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key in ('input', 'output', 'cacheRead', 'cacheWrite', 'totalTokens'):
        value = raw.get(key)
        if known(value):
            result[key] = value
    if raw.get('inputIncludesCache') is True and known(result.get('input')):
        result['inputIncludesCache'] = True
    return result


def usage_totals(raw):
    """Return (input_tokens, output_tokens) or (None, None) when unknown.

    A partial cache observation must not be rounded into a full total. When the
    provider explicitly reports ``inputIncludesCache`` true, ``input`` already
    accounts for cache reads/writes and is returned as-is (no double counting).
    Otherwise, legacy behavior applies: the input total is
    input+cacheRead+cacheWrite only when *all three* fields are known; if any
    is missing the input side stays unknown (None) rather than pretending the
    missing cache fields were zero. Output is unknown unless reported.
    """
    usage = normalize_usage(raw)
    if usage.get('inputIncludesCache') is True:
        # The provider already counted cache reads/writes inside `input`; adding
        # cacheRead/cacheWrite here would double count, so return `input` as-is.
        input_total = usage.get('input')
    elif all(key in usage for key in ('input', 'cacheRead', 'cacheWrite')):
        input_total = usage['input'] + usage['cacheRead'] + usage['cacheWrite']
    else:
        input_total = None
    return (input_total, usage.get('output'))


class ModelError(RuntimeError):
    """Bounded model failure. `.safe` is a whitelist-only summary.

    Subclasses RuntimeError so existing outer handlers (for example
    ``Evolution.process``) that catch RuntimeError keep capturing model
    failures instead of letting them escape.

    The exception never carries raw provider text, headers, or secrets: there
    is no free-form detail field at all, only whitelisted codes and an
    optional structured byte count for the local input guard. `usage` holds
    whatever was *known* before the failure (possibly empty, and possibly
    partial); it is never padded with fake zeros.
    """

    # Strictly whitelisted prior labels; anything else is dropped so no raw
    # provider text can enter the ledger through this channel.
    PRIOR_CODES = {
        'rate_limited': 'prior_rate_limited',
        'network_error': 'prior_network_error',
        'authentication_error': 'prior_authentication_error',
        'provider_error': 'prior_provider_error',
    }

    def __init__(self, category, code='provider_error', stop_reason=None,
                 status=None, usage=None, bytes_limit=None, bytes_actual=None,
                 prior_error_code=None):
        if category not in CATEGORIES:
            category = 'provider_failure'
        self.category = category
        self.code = safe_code(code)
        self.stop_reason = safe_stop_reason(stop_reason)
        self.status = safe_status(status)
        self.usage = normalize_usage(usage)
        # Whitelist-map the prior label; unknown/None becomes absent.
        self.prior_error_code = self.PRIOR_CODES.get(prior_error_code)
        # Structured, bounded numeric context for the local input guard. Only
        # integers are accepted; never provider text. These are *counts*, not
        # free-form detail.
        self.bytes_limit = bytes_limit if known(bytes_limit) else None
        self.bytes_actual = bytes_actual if known(bytes_actual) else None
        super().__init__(self.summary())

    @property
    def safe(self):
        return {'category': self.category, 'code': self.code,
                'stop_reason': self.stop_reason, 'status': self.status,
                'bytes_limit': self.bytes_limit, 'bytes_actual': self.bytes_actual,
                'prior_error_code': self.prior_error_code}

    def summary(self):
        """Compact safe label for ledger `error` columns, no free-form text."""
        parts = [self.category, self.code]
        if self.stop_reason:
            parts.append('stop=' + self.stop_reason)
        if self.status is not None:
            parts.append('status=' + str(self.status))
        if self.prior_error_code:
            parts.append('prior=' + self.prior_error_code)
        if self.bytes_actual is not None and self.bytes_limit is not None:
            parts.append('bytes=%d>%d' % (self.bytes_actual, self.bytes_limit))
        elif self.bytes_actual is not None:
            parts.append('bytes=%d' % self.bytes_actual)
        return '; '.join(parts)


def from_exception(exc, category=None, usage=None):
    """Coerce any caught exception into a bounded ModelError.

    Existing exception capture stays compatible: known subclasses keep their
    category, anything else becomes a generic provider_failure. Raw `str(exc)`
    is never copied into the safe fields.
    """
    if isinstance(exc, ModelError):
        if usage is not None:
            merged = normalize_usage(usage)
            if merged:
                exc.usage = merged
        return exc
    category = category or 'provider_failure'
    if isinstance(exc, (TimeoutError,)):
        category = 'timeout'
    return ModelError(category, usage=usage)


def error_label(exc):
    """Safe, bounded ledger label for a caught exception."""
    return from_exception(exc).summary()
