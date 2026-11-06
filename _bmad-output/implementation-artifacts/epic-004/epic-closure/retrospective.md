# Retrospective: epic E004 (single sprint, combined closure)

Stories: E004-S01-001 exclusions (6f3c5c4), E004-S01-002 v0 fixtures (6defdbe), E004-S01-003 encryption and inner-Manifest binding (3a1d1eb). Velocity 3 of 3. Commits interleaved with E005's merges.

## Summary
All three stories shipped and passed their gates on 3.11 and 3.14. The epic gate then found one reachable HIGH (a plain Archive substituted for an encrypted one is accepted by restore) and two reachable MEDIUMs (a tar header-body memory bomb; an `age` binary resolved through a relative PATH), all fixed with regression tests proven to fail without the fix. The pattern that recurs is the one E005 had: a mechanism proven through an injected seam with no production caller (FR-5 exclusions), left to a later epic by story text only.

## Learnings
- Untrusted-Archive reads need a bound on every stdlib parser step, not only on member data: `tarfile` reads GNU/pax header bodies unbounded. Per-story reviews of streaming code asserted "bounded memory" from the member path only.
- A guard's reach drifts the same way a rule's does: three of this epic's findings (AR-1, AR-7, AD-5) were guards whose hand-kept spelling lists missed ordinary variants. Plant each spelling the guard claims to catch.
- Downgrade is a separate threat from tamper. The ADR-0024 binding protects encrypted Archives; it cannot stop an attacker choosing plain. The operator needs a way to say "only encrypted", now `restore --require-encrypted`.
- Integration tests were skipped by the fixer for want of MySQL. Closure ran them against a throwaway mysql:8.0 with client binaries lifted from the image; worth a `make` target.
- Planning debt keeps landing on E008-S01-001: it now carries FR-5 reach, the encryption passphrase floor and the exclusion wiring. Consider splitting it.
