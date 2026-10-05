# Output Layout

All deliverables use `output/`; do not create a second `outputs/` directory.

## New Topics

```sh
python3 cards.py --topic "餐廳點餐" --description "內用點餐、客製需求與付款" --count 50
```

New topics default to the seven-column video curriculum:
`id, word_en, word_ipa, word_cn, tips, sentence_en, sentence_cn`.
`sentence_ipa`, `vocab`/`Core_Vocab`, and `Tone` are no longer generated or validated
in this flow. Scenario, tier, task, role and progression remain internal planning
data; Scenario/Level/Tone/Core_Vocab are not exported to Excel.
Each topic has 3-4 real scenarios,
balanced to within one card. Basic/advanced counts are 60/40, rounded to the nearest
whole card (50 cards = 30 basic + 20 advanced). Same-meaning cards are limited to
one basic and one advanced; pairing is optional, not required for every meaning.
Language, brief-required tone, main-line IPA, role and semantic audits run before export.
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
One consolidated audit checks language, translations, assigned task/speaker and
cross-card outcomes. It distinguishes permission from requirements,
allowances from declarations, and information requests from procedural guidance.
Each English field is rated independently. Advanced drafts must pass this check
before IPA filling; a harder main line cannot rescue a basic example. Valid drafts
and difficulty checks survive retries, and only unresolved IDs are requested again.
The consolidated audit uses `OPENAI_CURRICULUM_REVIEW_MODEL` (default `gpt-5-nano`);
focused difficulty and positive-synonym confirmations use
`OPENAI_CURRICULUM_SEMANTIC_MODEL`. Semantic
review time budgets scale with deck size, capped at 300 seconds and bounded by
the existing overall generation deadline.
Candidate synonyms proposed by the audit are verified in batches of at most 32
pairs before groups are merged; shared labels alone cannot merge different
requested information. A group of N candidates uses representative and adjacent
links (at most 2N-3), not all N*(N-1)/2 comparisons. Shared generic verbs or broad
planner labels no longer expand the final card review into an exhaustive pair audit.
Positive pairs still require independent source-grounded confirmation. Once
confirmed equivalence connects two tasks, redundant comparisons inside that group
are skipped. Planning stops the current audit as soon as confirmed duplicates
violate the one-basic/one-advanced rule, then replaces only the surplus tasks.
One task at each difficulty is retained; unchanged tasks, their independent audits,
and cached pair checks are reused. Localized audit/replacement requests use smaller
completion limits, rather than requesting a whole new deck for one bad task.
Progress reports show candidate counts, batch numbers and reused checks. Cached workbooks must
have matching content and a supported review version (11 or 12). Older completed
12-column workbooks are preserved without regenerating them. Unfinished decks
retain good cards and pass the new review before export.
API spending and repeated failures are bounded; failed review never
publishes an unchecked workbook.
AI audits are safeguards, not a guarantee of pedagogical correctness. Inspect the
finished content before using it in lessons. A failed run keeps its checkpoint;
it is not a completed workbook, and changing the model cannot fix missing API access.

Intermediate JSON files now live under `temp/cache/curriculum/`, keyed by their
absolute output path so identically named custom outputs do not share checkpoints.
The planning path is printed during execution. JSON is local resume/cache data,
not another AI-generated deliverable, and writing it has no API charge.
Existing sidecars beside old Excel files are copied into the cache on first reuse;
the originals remain untouched. Do not remove this cache if you need to resume.
`--plan-only` saves an editable `.plan.json` in the cache; `--plan-file` loads that exact new
curriculum plan. Old pain-point plans require `--legacy` and are never silently reused.
New runs save each validated planning step in `.xlsx.planning.json`, including
scenarios, tasks and the independent audit. Valid pair checks are saved in
`.xlsx.pairs.json`, keyed by actual content, brief, model and review policy. A timeout
or interruption retains these results and any pending task repairs.
Rerunning with the same topic, description, count and output automatically continues
unfinished planning or generation. `--resume` also works before a final plan exists.
Changed inputs/settings invalidate planning checkpoints; explicit `--resume`
rejects mismatched inputs. The former three-round stop no longer applies to planning,
task replacement, drafting, field repair, difficulty checks, or final review. Old
planning checkpoints with `attempt=3` can continue without `--force`.
Each execution shares a limit of 80 attempted API requests (`CARD_API_MAX_REQUESTS`)
and 300,000 input-plus-completion tokens (`CARD_API_TOKEN_BUDGET`), including transport
retries and model/key fallbacks. Before each attempt the program reserves a conservative
UTF-8 input/schema bound plus its maximum completion allowance. Successful responses
settle against `usage.total_tokens`; failed or usage-less responses retain the full
reservation. A request that would exceed either limit is not sent. These are token/request
limits, not dollar estimates; model pricing and input/output rates still affect cost.
Manual reruns receive a new budget, so repeated reruns can still accumulate charges.
Repeatedly submitting already rejected proposals stops after four consecutive repeats
(`CARD_STALLED_RETRY_LIMIT`); new proposals or reduced pending work may continue within
the shared budget. Budget or stalled-work stops save progress and do not publish an
unchecked workbook. The 900-second default overall deadline remains
(`CARD_GENERATION_TIMEOUT` overrides it).
`--resume` uses `.planning.json` or `.curriculum.json` as appropriate. `--avoid` excludes
previous decks. Existing Excel files are not overwritten without `--force`.
Final review rejections are saved so a resumed run fixes the rejected cards rather
than starting the same complete-deck audit again. Replaced duplicate tasks must
independently demonstrate a genuinely new outcome before they enter the plan.
`--force` backs up the workbook, plan, planning/generation checkpoints and pair
checks before rebuilding.
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

`main.py` accepts both new seven-column and existing eight/twelve-column Excel.
Its editable review copy is under `temp/cache/reviews/`, and its local card snapshot
is `temp/cache/video/data.json`, not `output/data.json`.
MP4 and YouTube descriptions remain in `output/`. External SRT files are disabled
by default; opt in with `python main.py --subtitles`. Internal timing records and
YouTube progress timestamps are still calculated even when SRT output is off.
Existing subtitles and JSON files are not deleted. SRT creation is local and free;
this toggle reduces output clutter, not OpenAI spending.
Cleanup backups are under `.cleanup-backups/output-cleanup-20261002/` at the
project root, with a manifest recording every original path, destination and hash.
No original content was permanently deleted; only empty directories were removed.
