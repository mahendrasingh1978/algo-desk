---
name: review
description: Review code changes before deploying. Check for bugs, edge cases, and regressions. Use before deploying any backend change.
disable-model-invocation: false
allowed-tools: Read, Grep, Glob
---

Review $ARGUMENTS before it goes to production.

## Checklist

### Correctness
- [ ] Does the logic match the stated intent?
- [ ] Are edge cases handled (None, empty list, zero, negative)?
- [ ] Are all DB sessions closed in try/finally?
- [ ] Are async calls properly awaited?

### AlgoDesk-specific
- [ ] Does any change affect the engine loop? If so, does day-reset still work?
- [ ] Does any change affect `conn.` calls? Are they broker-agnostic?
- [ ] Are new config values read from `state.config` not hardcoded?
- [ ] Does the Telegram notification still fire correctly (inside the right if block)?
- [ ] Any new list/dict that grows in a loop — is it bounded?

### Security
- [ ] No secrets or tokens in logs
- [ ] No raw SQL strings (use ORM)
- [ ] User-supplied input is validated before DB write

### Frontend
- [ ] Are admin-only sections guarded with role check?
- [ ] Does the change work for both paper and live mode?

## Output

List each issue found as:
- **[severity]** Description — file:line — suggested fix

If nothing found: "✅ Looks good to deploy"
