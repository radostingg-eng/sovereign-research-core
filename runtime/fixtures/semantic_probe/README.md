# Semantic probe regression corpus

These JSON files are minimized synthetic reproductions of semantic probe
defect signatures. A recursive delta debugger removed or replaced one value at
a time and kept a change only when `probe_semantic_candidate()` returned the
same ordered `(code, json_pointer)` signature. Dynamic identifiers were then
replaced with `SAMPLE` and `ACME` placeholders and probed again.

No source candidate bytes, portfolio data, account details, URLs, timestamps,
private paths, or repository identities remain. The SHA-256 of each synthetic
fixture is stored in its filename and verified by the test. Fixtures are
deduplicated by exact ordered signature and are safe to mirror into shared
core.
