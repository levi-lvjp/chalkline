# Contributor guidance

- The model proposes; code validates and commits. Keep state transitions, money arithmetic (integer minor units), approval hashes and dedupe in code, never in prompts.
- Customer-supplied text is untrusted input. Customers must never be able to approve anything.
- Use fictional data in tests and docs.
- Run the offline tests from the repository root: `python -m pytest tests` (needs the Hermes source and dependencies importable).
- Every behaviour change needs a regression test.
- Documentation and code comments are in English.
