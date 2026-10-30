# Agent revision interface

The default MCP surface is four tools: `lyx_read`, `lyx_apply_edits`,
`lyx_compare`, and `lyx_job`. State, export, validation, PDF inspection, and
artifact retrieval are read modes; mutation types belong to one transaction.
The old Python service methods remain available during migration.

Reads parse the saved source without opening a LyX buffer. Paragraphs and insets
have IDs bound to the source SHA256. A bounded response contains state, outline,
requested paragraphs and typed inset tokens. Native text/LaTeX exports remain
explicit requests. The index is reused until the source hash changes.

All mutations require `expected_sha256`. Preflight resolves every operation on
one index and collects errors before work starts. Source patches are applied in
descending offset order to a disposable candidate. The server owns revision
markup generation; clients never splice markers themselves. LyX loads and saves
the candidate once, verification checks accepted and rejected content, preserved
insets, and revision status/author/timestamps, and one compilation produces all artifacts. The original is replaced
atomically only after validation and a final version check. Cancellation before
commit leaves it untouched; cancellation after commit reports committed.
Paragraph layout, split/join and move operations explicitly report their visible
tracking limits rather than promising unsupported LyX revision semantics.

A build records source and dependency hashes, retained PDF/TeX/log/auxiliary
files, diagnostics, and reference mappings. Subsequent reads, validation and
exports reuse a matching build. Artifact contents and logs are returned only on
request. Build IDs survive an internal LyX restart, within the server session.

Operations run under one end-to-end deadline, including queueing and IPC.
Bounded tool waiting returns a job ID. One job tool supports status, bounded wait
and cancellation. Terminal results include the stage, diagnostic and transaction
outcome; a failed recovery blocks mutation until reviewed. The original stalled
read has no established root cause: command and file polling previously had
separate time budgets, and cancelling IPC left a broken session without shared
recovery. Regressions exercise these concrete failure modes.

Comparison returns small version-bound source hunks and current-manuscript
conflicts. Three-way mapping only reapplies changes whose base context remains
unchanged. Conflicting or already applied hunks cannot silently overwrite the
author's current work. PDF annotations and rendering are optional read modes.

Production manuscripts are reference inputs only in development. Regression
tests mutate isolated copies and compare reference hashes before and after.

Replacements spanning previous revisions use the accepted content of the selected
span as their baseline and report `rebased_prior_revisions`. Outside that span,
previous revisions remain intact. Both accepted and rejected projections are
verified after the native LyX save. `validation.revisions_verified` also confirms
that accepted text retains its planned revision status, author identity and
timestamp. Author-ID renumbering and omitted empty email fields are normalized.

