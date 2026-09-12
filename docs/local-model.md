# Local worker discovery — 2026-09-13

Observed interface: http://127.0.0.1:6996/v1, OpenAI-compatible
GET /models and POST /chat/completions. No API key required in the tested local setup.
The runner supports an optional LOCAL_LLM_API_KEY without writing it into logs.
Connection is restricted to loopback, proxy bypass enabled, redirects rejected.

Requested model: qwen3.8-27b|IQ3_XXS|10934860704.
Initial inference reported Qwen3.8-27B-UD-IQ3_XXS.gguf, fingerprint b9608-70b54e140.
The supplied C:/LocalsLllm/unsloth directory contains UD-IQ3_S, a different quantization.
Do not silently swap models. No second server is started and the original config is unchanged.

Supplied profile: ctx=16384, parallel=1, ngl=99, K/V=q8_0, flash attention=on,
speculative=nextn, draft maximum=3/minimum=1, cache-ram=0, ctx-checkpoints=4.
Saved preset has ctx=8192. useMmproj=true conflicts in intent with extra --no-mmproj
and --no-mmproj-auto; actual server flag precedence was not verified here.
These are configuration observations, not proof of effective runtime settings or optimality.

Live follow-up: llama-server.exe is the bundled llama.cpp-b9608-rocm runtime behind
turboLLM (backend port 8081). Its initial launch argument and /props confirmed 16384;
after the user changed runtime settings, /props confirmed n_ctx=51712. The quantization
is still IQ3_XXS. The live process arguments subsequently confirmed --cache-type-k q4_0,
--cache-type-v q4_0, -c 51712, -ngl 99 and --parallel 1.
No claim of optimal RAM/VRAM usage is made.

Initial WORKER_OK probe: 59 prompt tokens, 38 completion tokens including reasoning,
1.95 s client elapsed, server decode ~27.96 tokens/s. A tiny probe is not a coding benchmark.
Non-streaming completions verified. Streaming and native tool calling are not required
by this adapter and have not been verified. No undocumented runtime behavior is required.

The generic runner defaults to 16384 context and 4096 maximum completion tokens, temperature 0.2
for bounded coding requests. This per-request setting does not modify the saved profile.
Input uses a conservative UTF-8 byte ceiling with output and framing reserves, not an exact
tokenizer count. Reject oversized contexts rather than silently truncating contracts.
Only task context and previous scoped edits/feedback are sent. The model returns a JSON
file envelope; host applies it. Reasoning may consume completion budget; incomplete answers
are failures. API timeout is configurable, default 180 seconds, and retries are bounded.

POC attempt 1 failed with finish_reason=length: 4096 completion tokens were consumed
by reasoning, with no content/file proposal. The installed chat template defaults to
xhigh when reasoning is enabled. This is why enlarging context alone does not fix it.
Two isolated probes succeeded with no reasoning block: nested
chat_template_kwargs.enable_thinking=false, and top-level reasoning_effort="off".
The adapter uses the latter, configured as LOCAL_LLM_REASONING_EFFORT=off in .env.
An empty setting omits this runtime-dependent parameter for portability. The current
.env.example uses the observed 51712 context limit; task context is still explicitly bounded.
The effect of UI-wide sliders was not independently verified; each worker request is explicit.

POC attempt 2 with reasoning_effort=off succeeded: 1535 prompt tokens, 871 completion
tokens, 28.875 seconds client elapsed, ~34.94 tokens/s server decode. Two permitted files
were applied, eight HTTP tests passed (four independent lead cases plus four worker cases),
cloud review passed and the branch was merged/cleaned up. Context and KV settings changed
between attempts, so this is a functional recovery observation, not a controlled speed benchmark.

GGUF files require an inference runtime; the file path itself is not an API.
If turboLLM is unavailable, first inspect its logs/runtime and model choice. An alternative
loopback OpenAI-compatible runtime can be configured in .env. Direct launching is a separate
measured environment operation, not an automatic fallback that competes for VRAM.
