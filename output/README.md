# Output Layout

All deliverables use `output/`; do not create a second `outputs/` directory.

## New Topics

```sh
python3 cards.py --topic "餐廳點餐" --description "內用點餐、客製需求與付款" --count 50
```

New topics default to the 12-column curriculum. Each topic has 3-4 real scenarios,
balanced to within one card. Basic/advanced counts are 60/40, rounded to the nearest
whole card (50 cards = 30 basic + 20 advanced). Same-meaning cards are limited to
one basic and one advanced; pairing is optional, not required for every meaning.
Language, tone, IPA, vocabulary, role and semantic audits run before export.
New-curriculum main lines allow up to 12 words and examples up to 14 so natural
grammar is not truncated; the legacy 8-word main-line limit is unchanged.
The new curriculum defaults to `gpt-5-nano`, which is available to the current
API project. Its author model can be overridden with
`OPENAI_CURRICULUM_AUTHOR_MODEL`; planning, independent copy review, corrective
retries and advanced drafts also have respective overrides:
`OPENAI_CURRICULUM_PLAN_MODEL`, `OPENAI_CURRICULUM_REVIEW_MODEL`,
`OPENAI_CURRICULUM_REPAIR_MODEL`, and `OPENAI_CURRICULUM_ADVANCED_MODEL`.
These settings do not change the legacy model configuration.
Main lines are locked by ID in the structured
output. Both advanced English fields are drafted before IPA/translation and
locked during field filling. Examples may paraphrase naturally; review checks both English fields
for matching meaning, role and actual difficulty, without requiring exact word order.
A separate focused audit compares the main line, example and their translations
against the assigned task and speaker. It distinguishes permission from requirements,
allowances from declarations, and information requests from procedural guidance.
Each English field is rated independently. Advanced drafts must pass this check
before IPA filling; a harder main line cannot rescue a basic example. Valid drafts
and difficulty checks survive retries, and only unresolved IDs are requested again.
Complete cross-card meaning classification and a separate cross-label synonym
audit default to `gpt-5-nano` with low reasoning effort; override classification
effort with `OPENAI_CURRICULUM_SEMANTIC_REASONING` or the model with
`OPENAI_CURRICULUM_SEMANTIC_MODEL`. Semantic
review time budgets scale with deck size, capped at 300 seconds and bounded by
the existing overall generation deadline.
Candidate synonyms are verified pair by pair before groups are merged; shared
labels alone cannot merge different requested information. Cached workbooks must
have passed the current review version. Retries are bounded; failed review never
publishes an unchecked workbook.
AI audits are safeguards, not a guarantee of pedagogical correctness. Inspect the
finished content before using it in lessons. A failed run keeps its checkpoint;
it is not a completed workbook, and changing the model cannot fix missing API access.

`--plan-only` saves an editable `.plan.json`; `--plan-file` loads that exact new
curriculum plan. Old pain-point plans require `--legacy` and are never silently reused.
`--resume` uses the adjacent `.curriculum.json` checkpoint. `--avoid` excludes
previous decks. Existing Excel files are not overwritten without `--force`.
Final review rejections are saved so a resumed run fixes the rejected cards rather
than starting the same complete-deck audit again. Replaced duplicate tasks must
independently demonstrate a genuinely new outcome before they enter the plan.
`--force` backs up the workbook, plan and checkpoint before rebuilding.
`--no-youtube` skips description generation. Rerunning an already verified deck
without that flag generates a missing description without rebuilding its Excel.
`--legacy` explicitly retains the
old 8-column flow. The specialized sales editor is not used for new topics.

- `推銷與隱形敲詐.xlsx`: current learning edition, 32 cards and 12 columns.
- `推銷與隱形敲詐.xlsx.editor.json`: paired content and editorial provenance.
- `learning-edit-20261001/original.xlsx`: preserved original 50-card source.
- `learning-edit-20261001/`: editable content, builder, verifier, historical report.
- `semantic-rerun-20261001/final/`: previous 50-card rerun, not the current edition.
- Other existing workbooks, plans, videos, subtitles, and descriptions are preserved.

Verify without rewriting the workbook:

```sh
python3 output/learning-edit-20261001/verify_learning_workbook.py "output/推銷與隱形敲詐.xlsx" --source output/learning-edit-20261001/original.xlsx --editorial
```

Regenerate only when intentionally replacing the current edition:

```sh
python3 output/learning-edit-20261001/build_reviewed_edition.py --replace-edition
```

Disposable previews and new reports go to `temp/`. Historical report preview names
refer to the original check; the old images are archived.
Cleanup backups are under `.cleanup-backups/output-cleanup-20261002/` at the
project root, with a manifest recording every original path, destination and hash.
No original content was permanently deleted; only empty directories were removed.
