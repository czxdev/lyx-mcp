---
name: lyx-paper-editing
description: Edit LyX papers through the isolated LyX MCP server with tracked changes, visible revisions in PDF, precise revision spans, and transactional compilation checks.
---

# Editing LyX papers

Use the `lyx-mcp-server` MCP tools for `.lyx` edits. Make both prose and structural changes through MCP. Do not run the old `check_lyx_changes.py`, splice `\change_*` markers into the source, or rewrite the paper with a separate script. The server starts its own LyX process and does not connect to a desktop instance.

## Workflow

1. Call `lyx_get_document_state` to record the target SHA256, revision settings, and autosave state. Use `lyx_read(format="text")` for accepted text and `lyx_read(format="latex")` to inspect formulas, citations, and structure. The target must be inside a configured `allowed_roots` directory.
2. Express each change as a clear semantic unit. Use `lyx_apply_edits` with `replace`, `delete`, `insert_before`, or `insert_after` for ordinary text. Batch nonoverlapping edits. Disambiguate repeated text with `context_before`, `context_after`, and `occurrence`.
3. A plain-text target cannot span a formula, citation, reference, note, table, ERT, or another inset. To transfer an already reviewed structural revision, use `lyx_import_revision_range` only when source and target have the same unique Section boundaries and the target range has no revisions. It replaces the entire Section range: compare all prose, formulas, citations, and tables in that range first.
4. If imported revisions are fragmented into many adjacent word edits, call `lyx_normalize_revisions` with their author ID, timestamp, and the current target SHA256. Inspect the reported number of merged groups and accepted ERT replacements. An unsafe `resizebox` ERT opening is accepted as a structural formatting change, so that particular change no longer appears as a deletion and insertion.
5. Keep `compile=true` by default. After each round, confirm `tracking_changes=true` and `output_changes=true` with `lyx_get_document_state`. Check accepted text and generated LaTeX with `lyx_read`, then compile the visible-revision PDF with `lyx_validate_revision`. On failure, read the MCP stage, error code, and rollback status; fix the cause, fetch the new SHA256, and retry.

An MCP edit transaction snapshots the source and checks accepted text, citation inset counts, and the PDF with revisions visible. It rolls back on failure. A result obtained with `compile=false` is not a deliverable paper. Citation keys must resolve in the bibliography; PDF compilation reports undefined citations.

## Choosing the revision span

Choose the smallest continuous span that expresses what changed to a reader. Do not turn a sentence rewrite into an alternating series of word deletions and insertions merely because a word-level diff can align some words.

- **One word or inflection:** Replace the whole word and preserve the rest of the sentence. For `achieve → achieves`, do not delete and reinsert the sentence. The server narrows shared prefixes and suffixes while protecting whole-word boundaries.
- **One continuous phrase:** Replace adjacent words that form one changed idea in a single edit. A short unchanged connector may belong inside the span when that makes the marked PDF readable. Avoid a separate revision for every word.
- **A sentence rewrite:** If the argument, structure, or most words change, replace the sentence once. Do not pair old and new words by position. Conversely, one changed word does not justify marking an otherwise unchanged sentence.
- **Separated changes:** Split edits across substantial unchanged text, different sentences, paragraphs, or inset boundaries. Do not join ordinary text across formulas or citations.
- **Read the marked result:** Inspect LaTeX or the PDF with `\output_changes true`. If a sentence rewrite appears as many adjacent `\lyxdeleted{...}\lyxadded{...}` fragments, combine the revision. If a small correction marks a long sentence, narrow it. Use the MCP normalization tool for existing fragmented revisions, never the old checking script.

In ordinary LyX text, including captions, write a numeric range with one ASCII hyphen: `10-50`, not the raw-TeX en-dash input `10--50`. For example, Figure 4 should read `Fork rate under nominal delays of 10-50 rounds.` The TeX `--` notation belongs only in raw LaTeX/ERT when appropriate. Do not copy TeX export punctuation back into a LyX prose edit. Treat a range correction such as `10--50 → 10-50` as one contiguous range revision, not a lone character edit.

`\change_inserted`, `\change_deleted`, and `\change_unchanged` switch LyX revision state; they are not ordinary TeX macros to concatenate by hand. Deleting or inserting an unmatched ERT macro opening such as `\resizebox{...}{!}{` can make TeX parsing fail when `\output_changes true`.

## LyX syntax the MCP tools can edit

Each form below has its own MCP tool test in `tests/test_lyx_syntax.py`. These are the currently verified forms. For other LyX syntax, work on a copy and extend the tests first.

| LyX source syntax | MCP operation | Boundary |
| --- | --- | --- |
| `\begin_layout Standard` paragraph | `lyx_apply_edits` | Continuous ordinary text |
| `\begin_layout Section` heading | `lyx_apply_edits` | Continuous heading text |
| `\begin_layout Subsection` heading | `lyx_apply_edits` | Continuous heading text |
| `\begin_layout Itemize` item | `lyx_apply_edits` | Ordinary text within one item |
| `\begin_layout Enumerate` item | `lyx_apply_edits` | Ordinary text within one item |
| `\begin_inset Formula $...$` | `lyx_import_revision_range` | Complete tracked formula inset |
| `\begin_inset CommandInset citation`, `key "..."` | `lyx_import_revision_range` | Complete citation inset; key must resolve |
| `\begin_inset CommandInset ref`, `reference "..."` | `lyx_import_revision_range` | Complete reference inset; label must exist |
| `\begin_inset Note Note` | `lyx_import_revision_range` | Complete Note inset; contents are absent from plain-text export |
| `\begin_inset Tabular` | `lyx_import_revision_range` | Tracked table range; compile each table |
| `\begin_inset ERT` | `lyx_import_revision_range`; sometimes `lyx_normalize_revisions` | Balanced TeX commands can be imported; an unmatched `resizebox` opening must be accepted as a structural revision |

Preserve matching `\begin_layout` / `\end_layout` and `\begin_inset` / `\end_inset` pairs. MCP rejects a plain-text search that crosses an inset. Edit the ordinary text on either side separately, or import a reviewed structural revision.

## Delivery checks

Record the target SHA256, merged revision group count, accepted ERT count, PDF export path, and any content still needing review. Confirm that the PDF compiles with `\output_changes true`, citations have no undefined warnings, and accepted text matches the intended edits. To retain a PDF, call `lyx_export(format="pdf2", output_path=...)`; the destination must be in `allowed_roots` and must not already exist.
