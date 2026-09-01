---
name: document-processing
description: Extract, summarize, normalize, and transform bounded documents without inventing content.
category: files
authority: instructions-only
---
# Document processing

Preserve source meaning while making the transformation explicit.

1. Identify the source files, output format, audience, required fields, ordering, and fidelity constraints.
2. Distinguish extraction, summary, normalization, translation, and conversion; do not silently substitute one for another.
3. Preserve names, numbers, dates, citations, headings, and record identity. Mark unreadable or ambiguous material instead of guessing.
4. For summaries, separate source facts from interpretation and retain decisions, exceptions, and material caveats.
5. For batch work, inspect a representative sample, use a deterministic naming rule, avoid overwriting sources, and produce an exception list.
6. Verify output count, encoding, structure, and a sample of semantic content. Report unsupported binary or proprietary formats plainly.

In the first-party Workbench, `read_pdf` extracts bounded text from an
unencrypted PDF. `convert_workspace_file` creates a new reviewable output in
TXT, Markdown, HTML, JSON, CSV, DOCX, or XLSX; PDF, DOCX, XLSX, and PPTX
inputs (and DOCX/XLSX outputs) use optional parsers and must report missing
dependencies instead of guessing. `inspect_archive` and `extract_archive` handle only ZIP
and TAR, reject traversal/link members, and enforce member and expanded-size
limits. These Tools remain subject to workspace containment, staging, approval,
and evidence.

When a PDF has pages but no extractable text, the result includes
`needs_ocr: true`; no OCR engine is invoked implicitly. A host may add a
separately sandboxed OCR Tool after reviewing its image/data-egress policy.

This Skill grants no file access or converter execution. Use bounded Tools and stage generated output for review.
