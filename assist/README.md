# Design notes and RFC archive

This directory preserves the reasoning that shaped LIPAS. It is useful for
contributors and for discussing future work, but it is **not** the public API
or semantic contract.

For the current 0.9.6 runtime, read [Execution Model](../docs/execution-model.md)
first, then the public README and `STABILITY.md`. Code and contract tests win
whenever an archived note differs from the implementation.

| Document | Status | How to read it |
|---|---|---|
| `one-calculus.md` | Conceptual foundation | Explains the claim/fold motivation; not a literal current data schema. |
| `three-rows.md` | Conceptual foundation | Explains the original row decomposition; not a public API reference. |
| `B3-NOTES.md` | Current design note | Background for the implemented Supervisor integration. |
| `A1.md` | Historical RFC draft | Replay design exploration; current replay behavior is implemented by `ToolReplayer` and documented in Execution Model. |
| `A2.md` | Unimplemented proposal | Claim-schema evolution and upcasters are not yet a supported migration protocol. |
| `A3.md` | Historical engineering note | Fold-purity rationale and testing ideas; not a complete runtime guarantee. |
| `A4.md` | Unimplemented proposal | Cross-Team capability delegation/attenuation is explicitly out of the current contract. |

Do not cite a draft in this directory as a supported feature without promoting
its precise, tested semantics into public documentation and code.
