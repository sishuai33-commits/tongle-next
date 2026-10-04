// Loaded only by Tongle's isolated runner, never installed globally.
//
// Three independent bounds:
//   * output cap  -> clamped max_tokens / max_completion_tokens,
//   * input bytes -> a hard local ceiling checked BEFORE the provider request,
//   * request count -> TONGLE_MODEL_MAX_REQUESTS provider-hook invocations.
//
// The byte ceiling defaults to "escaped-payload bytes + 6000": it is a headroom
// guard around the very prompt that was already assembled, deliberately NOT a
// fixed constant and deliberately NOT derived from any token estimate. An
// explicit TONGLE_MODEL_MAX_INPUT_BYTES pins a fixed cap instead.
//
// On rejection this extension never asks the model and never mutates the
// payload into a synthetic request. It writes a bounded JSON error to stderr
// with a synchronous write (fs.writeSync) so the line is flushed before the
// process exits, then terminates this dedicated worker subprocess
// synchronously via process.exit(7): no provider request is sent, nothing
// global is affected, and the parent reliably sees the diagnostic.
//
// TONGLE_MODEL_MAX_REQUESTS (default 1, positive safe integer, at most 32)
// counts before_provider_request invocations. When the count would exceed the
// limit, the extension writes a bounded code=request_limit line and exits 8
// synchronously. This is NOT a hard cap on provider HTTP traffic: Pi's hook is
// only observable per provider request attempt, so internal HTTP retries that
// Pi performs inside a single attempt are invisible here. An invalid value
// fails closed before any request. Enforcement never throws (Pi catches hook
// exceptions), so termination always happens via process.exit.
//
// Thinking control, scoped strictly to the GLM 5.3 Flash custom provider:
//   * TONGLE_MODEL_PROVIDER=zhipu + TONGLE_MODEL_MODEL=glm-5.3-flash +
//     TONGLE_MODEL_THINKING=off -> thinking:{type:"disabled"} (live endpoint
//     rejects disabling via absent effort with 400/1210).
//   * same provider/model with thinking minimal|low|medium|high ->
//     thinking:{type:"adaptive"} plus output_config.effort (minimal/low ->
//     "low", medium/high -> "high"). Existing output_config fields are
//     preserved. This is required because models.json sets no
//     forceAdaptiveThinking, so Pi's --thinking maps to the legacy
//     budget_tokens path instead of output_config.effort.
// Any other provider/model, and zhipu without an explicit model match, is left
// completely untouched so no other provider protocol changes.
import fs from "node:fs";

// Bounded, machine-readable diagnostic. Only whitelisted fields; the message
// never contains the prompt, environment, or any raw provider response.
function reject(code, exitCode, extra = {}) {
  try { fs.writeSync(2, JSON.stringify({ error: 'model request rejected', code, ...extra }) + '\n'); }
  finally { process.exit(exitCode); }
}

export default function(pi: any) {
  const rawMax = process.env.TONGLE_MODEL_MAX_REQUESTS ?? '1';
  const parsedMax = /^[0-9]+$/.test(rawMax.trim()) ? Number(rawMax.trim()) : NaN;
  if (!Number.isSafeInteger(parsedMax) || parsedMax < 1 || parsedMax > 32) {
    reject('request_limit', 8, { limit: null });
  }
  const maxRequests = parsedMax;
  let requests = 0;
  pi.on("before_provider_request", (event: any) => {
    requests += 1;
    if (requests > maxRequests) {
      reject('request_limit', 8, { limit: maxRequests });
    }
    const payload = event.payload as Record<string, unknown>;
    const raw = Number(process.env.TONGLE_MODEL_MAX_OUTPUT ?? 4096);
    const limit = Number.isFinite(raw) ? Math.max(256, Math.min(32768, Math.trunc(raw))) : 4096;
    const serialized = JSON.stringify(payload);
    const bytes = Buffer.byteLength(serialized, 'utf8');
    const override = process.env.TONGLE_MODEL_MAX_INPUT_BYTES;
    const ceiling = override ? Number(override) : bytes + 6000;
    if (!Number.isFinite(ceiling) || bytes > ceiling) {
      try { fs.writeSync(2, JSON.stringify({
        error: 'model input exceeds local byte budget',
        code: 'input_too_large',
        bytes_actual: bytes,
        bytes_limit: Number.isFinite(ceiling) ? ceiling : null
      }) + '\n'); } finally { process.exit(7); }
    }
    const bounded = 'max_completion_tokens' in payload
      ? { ...payload, max_completion_tokens: limit }
      : { ...payload, max_tokens: limit };
    const thinking = process.env.TONGLE_MODEL_THINKING;
    const isGlmFlash = process.env.TONGLE_MODEL_PROVIDER === 'zhipu'
      && process.env.TONGLE_MODEL_MODEL === 'glm-5.3-flash';
    if (isGlmFlash && thinking === 'off') {
      return { ...bounded, thinking: { type: 'disabled' } };
    }
    if (isGlmFlash && thinking && thinking !== 'off') {
      const effort = (thinking === 'medium' || thinking === 'high') ? 'high' : 'low';
      const prior = (payload.output_config && typeof payload.output_config === 'object')
        ? payload.output_config as Record<string, unknown> : {};
      return { ...bounded, thinking: { type: 'adaptive' },
               output_config: { ...prior, effort } };
    }
    return bounded;
  });
}
