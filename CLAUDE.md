# ap-agent

Agentic AP invoice matching and payment scheduling.

## Glossary — one word per concept, project-wide
- Purchase order = `po` / `PurchaseOrder`. Not order, not purchase.
- Goods receipt = `grn` / `GoodsReceipt`. Not receipt, not delivery.
- Invoice = `invoice`. Line item = `line_item`. Not line, not item.
- Tolerance = `tolerance`. Not threshold — `threshold` is reserved for the
  manual-review amount cutoff.
- Difference = `discrepancy`. Not mismatch, not diff, not variance.

## Hard constraints
- Money is always an int number of cents. Never float.
- Quantity is always in the base unit. Conversion happens once, in the
  extraction layer, and nowhere else.
- Percentage fields are percentage points: `2.0` means 2%, not 200%.
- Dates are ISO strings, not date objects.
- `schemas.py` is the single source of truth. Do not redefine these models
  anywhere else. Changing it changes the contract for all three modules.
- Every agent decision must carry its full ordered `tool_calls` trail.
- API keys live in `.env` only, read via `os.getenv()`. Never hard-coded,
  never passed as function arguments.
- No business logic in the frontend. Tolerance checks and money arithmetic
  happen in Python only.

## Metrics definitions
- STP rate = APPROVE count / total invoices. HOLD does not count — no human
  touched it, but the process is not finished.
- Touchless rate = (APPROVE + HOLD + EMAIL) / total. All three were decided
  without a human at that moment; EMAIL is a query the system sent itself.
  Report both this and STP; one number alone invites suspicion.
- False-approve rate = invoices approved that should not have been. This is
  the number that matters most: the risk of automation is wrong payment,
  not slowness.

## Provider notes
- `LLM_PROVIDER` is one of: `anthropic`, `deepseek`, `groq`, `openai`,
  `bedrock` (placeholder until credits arrive). Each provider reads its own
  key env var (`DEEPSEEK_API_KEY`, `GROQ_API_KEY`, ...) — see `.env.example`.
- The Anthropic code path also serves DeepSeek via
  `https://api.deepseek.com/anthropic`. Only `ANTHROPIC_BASE_URL` and
  `ANTHROPIC_MODEL` change. Verified working, including tool calling.
- DeepSeek ignores `cache_control` entirely. It has its own automatic disk
  cache instead. Keep the markers — they work on Anthropic.
- DeepSeek does not support `document` or `image` content blocks. If we ever
  want to feed a PDF directly to the model, that path is Anthropic-only.
- DeepSeek strict mode (base_url `/beta`, `"strict": true` per function)
  is untested but worth trying — it would eliminate a class of JSON parse
  failures. Constraint: all object properties must be required and
  `additionalProperties` must be false.
- Tool schemas: avoid `minLength`, `maxLength`, `minItems`, `maxItems`.
  DeepSeek strict mode rejects them.

## Open questions — decide after the 8/14 problem statement
- Tolerance values in `ToleranceConfig` are placeholders. Confirm against the
  actual scenario.
- `qty` is `int`, assuming countable goods. If the problem set involves
  weight- or length-priced items, switch to integer thousandths of the base
  unit — same rule as money.
- Defect mix for the synthetic test set is not yet fixed.
- Module ownership is not yet assigned. See below.

## Module ownership
To be assigned at kickoff. Until then, raise before editing a module you did
not write.

## Conventions
- All comments and docstrings in English.
- Comments explain *why*, and name the alternative that was rejected.
- ruff handles formatting. Do not argue about style; run the hook.
