# Local Factory design

**Experimental feature branch:** The tested macOS environment blocked native process verification with `sysctl` `EPERM`. Windows has partial test evidence, while independent workflow review blocked verification security because the changed checker verified itself. This branch is not cross-host qualified or approved for production use.

The Factory is a small evidence tool with three operations: prepare an isolated baseline and work copy, check the exact candidate under fixed limits, and report a separately obtained review. The normal developer or agent owns planning and implementation. The Factory does not create model sessions or decide that a reviewer is independent.

Preparation fixes the request, tool identities, trusted local Python and stdlib observations, baseline inventory and run identity. Checking consumes the run before launching children, captures complete B and C Git trees and the binary diff, runs the fixed test bootstrap against C, and checks source and helper immutability. The result records process termination and a review-packet hash. An unknown process outcome blocks positive results and later runs in the collection.

For a tracked checkout, the operator supplies its complete file inventory and independently established Git tree. Root `.git` is explicitly excluded; unlisted entries are refused. An explicit candidate source root and an independently inventoried local dependency root avoid ambient installed-code imports. The review packet includes the dependency bytes. A collection-level active marker precedes child launch and remains held when a check cannot publish a known-ended terminal record. Recovery is an external operator decision, and a new parent is not a bypass for an unresolved operation.

The macOS supervisor uses owned process groups and a per-entry custody gate. Its record covers the direct child, sampled descendants and observed groups; it cannot rule out an unobserved escape. The Windows supervisor assigns a suspended child to an owned Job before resume and records the final Job state. Both must retain the original deadline and cleanup reserve. Observed results are host-specific; a mock or another operating system's success is not native verification. Filesystem limits and trusted local code are not a hostile-code sandbox.

Review import binds the run, packet, candidate, implementation session, a distinct reviewer session, evidence hash and four explicit verdicts. The review is a local assertion, not a cryptographic identity guarantee. New clean collections support serialized imports with durable attempt markers. Historical claim-only partial imports are not repaired; do not rely on automatic detection of those old states.

The install and invocation contract, platform restrictions, evidence paths and restart rules are in [FACTORY_USAGE.md](FACTORY_USAGE.md).
