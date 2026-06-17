## Summary
<!-- One paragraph describing the change. Link any related issue. -->

## Motivation / Context
<!-- Why is this needed? How does it align with Maximum Nativeness? -->

## Changes
- [ ] ...

## Checklist (Required)
- [ ] I have read [AGENTS.md](AGENTS.md) and followed "Maximum Nativeness" + all rules.
- [ ] Changes respect core invariants:
  - [ ] Web dashboard (`web/`) still never touches or exposes secrets.
  - [ ] Media sentinel / direct delivery pattern (`media/delivery.py`) is untouched (if media touched).
  - [ ] Activation policy, per-user rate limiting (6/60s), and guild whitelist (`ALLOWED_GUILD_IDS`) behavior unchanged.
  - [ ] OAuth handling and `oauth/` storage unchanged.
  - [ ] Decoupled bot vs web processes preserved.
- [ ] I have added or updated tests where appropriate.
- [ ] All tests pass: `pytest -q` and `python scripts/check.py --skip-docker`.
- [ ] I ran `groksito --check` (or equivalent) locally.
- [ ] Documentation updated (README, CONTRIBUTING, etc.) if user-facing or process changed.
- [ ] No unrelated files or scope creep.
- [ ] I understand this project is MIT licensed and contributions fall under it.

## Testing Performed
<!-- Describe manual + automated verification. -->

## Screenshots / Examples (if UI or behavior change)

## Additional Notes
<!-- Anything for reviewers (e.g. "small prompt-only change", "docs only"). -->

<!-- Thank you! -->
