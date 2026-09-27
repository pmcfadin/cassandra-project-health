# Public benchmark: TypeSafe Jev vs. public human-labeled datasets

DECISIONS.md D22, D23; COMMUNITY-HEALTH.md §1, §6; issue #89. Primary classifier benchmark: `project-health benchmark-public` scores the pinned TypeSafe Jev classifier (D17) against public, human-labeled datasets whose own label schemes are mapped onto this project's 12 message-level labels (`label_mapping_v1.yaml`, `docs/plans/2026-09-27-public-benchmark-datasets.md`).

**Aggregate only.** No item text, no item/message ids, and no per-item detail appear anywhere below -- per COMMUNITY-HEALTH.md §7's "quotations: default none" and D23's "only aggregate results are published." Dataset text is fetched into a local cache directory at run time and is never committed to this repo or redistributed.

- Generated at: 2026-09-27T17:32:38.758996+00:00

## Cost and latency

- Calls made: 0 (cache hits: 16487)
- Input / output tokens: 0 / 0
- Estimated cost (USD): 0.0
- Elapsed seconds: 0.33035724982619286
- Mean latency per call (seconds): None
- Run status: completed
- classifier_version / question_set_version / model_id: 1.0.0 / 1 / jev-1.13.0

## Thresholds

`questions_v1.yaml` (v1) pins every message-level label's `threshold: null` -- no label has a calibrated production threshold yet (issue #47 calibrates one against the frozen owner/rater benchmark, D23's "gaps" stratum). Every table below therefore reports the **best-F1 threshold found in this benchmark** (ties broken by higher precision, then the lower threshold, matching `pilot/stats.best_threshold_by_f1`) rather than a value at a pinned gate threshold, which does not exist yet.

## Datasets not run

Per issue #89: a dataset that fails to download, needs a form/login, or whose format differs from what was expected is reported here, never given a fabricated loader.

| Dataset | Reason |
|---|---|
| DEBAGREEMENT (Pougue-Biyong et al., 2021) | No working download exists as of 2026-09-27. The paper's own canonical dataset_url (a Scale AI "Open Datasets" page) has been repurposed for an unrelated product; the current live page (scale.com/research/debagreement:-a-comment-reply-dataset-for-disagreement-detection-in-online-debates, confirmed HTTP 200) links only to a research write-up, not a data file. Checked and exhausted: figshare search (no hits), GitHub repo-name guesses and a GitHub code/repo search (the only matching repos contain no raw data file), Zenodo search (no hits), arXiv API search (no hits -- this is an OpenReview-only publication, never posted to arXiv), and the OpenReview API itself (403, requires auth). No fabricated URL is pinned here; this dataset is reported as blocked, not implemented. |

## Datasets run

`n_sampled` is how many items this run drew from the dataset; `n_distinct_inputs` is how many of those are byte-distinct after preprocessing (two different items can read identically, e.g. two "LGTM" comments, and hash to the same classifier input); `n_evaluated` is how many of the `n_sampled` items actually resolved to a classification record. `n_evaluated` should equal `n_sampled` whenever the run completed without hitting the cost cap -- every sampled item, including ones sharing an input with another, is joined back to its record by that shared input's hash (orchestrator review of issue #89 caught and fixed a bug where duplicate-input items were silently excluded here).

| Dataset | Citation | License | n_sampled | n_distinct_inputs | n_evaluated | Population n |
|---|---|---|---|---|---|---|
| Ferreira, Adams & Cheng -- GitHub locked issues ("How heated is it?", 2022) | Ferreira, Adams & Cheng, "How heated is it? Understanding GitHub locked issues", MSR 2022, arXiv:2204.00155. Dataset republished as Ferreira, Rafiq & Cheng, "Incivility Detection in Open Source Code Review and Issue Discussions", Journal of Systems and Software, 2024, figshare DOI 10.6084/m9.figshare.24603237.v1. | CC BY 4.0 (figshare API license.name, verified live 2026-09-27; see ferreira_lkml's note on the GPL-3.0-vs-CC-BY-4.0 conflict between the GitHub repo and the figshare copy of this same data) | 2000 | 1952 | 2000 | 5115 |
| Ferreira, Cheng & Adams -- LKML incivility ("Shut the f**k up", 2021) | Ferreira, Cheng & Adams, "The 'Shut the f**k up' Phenomenon: Characterizing Incivility in Open Source Code Review Discussions", PACMHCI 5(CSCW2), 2021, arXiv:2108.09905. Dataset republished as Ferreira, Rafiq & Cheng, "Incivility Detection in Open Source Code Review and Issue Discussions", Journal of Systems and Software, 2024, figshare DOI 10.6084/m9.figshare.24603237.v1. | CC BY 4.0 (figshare API license.name, verified live 2026-09-27; note the GitHub repo iferreiradev/incivility_detection separately reports GPL-3.0 for the same data -- the figshare copy's CC BY 4.0 is used here, per the research doc's own resolution of that conflict) | 1495 | 1466 | 1495 | 1495 |
| TalkDown (Wang & Potts, 2019) | Wang & Potts, "TalkDown: A Corpus for Detecting Condescension in the Alt-Right", EMNLP-IJCNLP 2019. github.com/zijwang/talkdown. | AGPL-3.0 (GitHub API license.spdx_id, verified live 2026-09-27; repo-level, no separate data-only license file) | 4992 | 4992 | 4992 | 4992 |
| ToxiCR (Sarker, Turzo, Dong & Bosu, 2023) | Sarker, Turzo, Dong & Bosu, "Automated Identification of Toxic Code Reviews Using ToxiCR", ACM TOSEM 32(1), 2023, arXiv:2202.13056. github.com/WSU-SEAL/ToxiCR. | GPL-3.0 (GitHub API license.spdx_id, verified live 2026-09-27; a software-copyleft concern for redistributing the tool/data, not a bar on publishing this benchmark's derived aggregate results) | 3000 | 2734 | 3000 | 19651 |
| Wikipedia Personal Attacks / Aggression (Wulczyn, Thain & Dixon, 2017) | Wulczyn, Thain & Dixon, "Ex Machina: Personal Attacks Seen at Scale", WWW 2017, arXiv:1610.08914. figshare DOI 10.6084/m9.figshare.4054689. | CC0 (figshare API license.name, verified live 2026-09-27; the paper itself is published under CC BY 4.0) | 5000 | 4997 | 5000 | 115864 |

## Ferreira, Adams & Cheng -- GitHub locked issues ("How heated is it?", 2022)

- Citation: Ferreira, Adams & Cheng, "How heated is it? Understanding GitHub locked issues", MSR 2022, arXiv:2204.00155. Dataset republished as Ferreira, Rafiq & Cheng, "Incivility Detection in Open Source Code Review and Issue Discussions", Journal of Systems and Software, 2024, figshare DOI 10.6084/m9.figshare.24603237.v1.
- License: CC BY 4.0 (figshare API license.name, verified live 2026-09-27; see ferreira_lkml's note on the GPL-3.0-vs-CC-BY-4.0 conflict between the GitHub repo and the figshare copy of this same data)
- Classifier source venue used: `github_pr_comment` -- questions_v1.yaml's message.source enum has no plain "github_issue_comment" value -- github_pr_comment is the closest of the three allowed venues (same platform and comment register; GitHub issue-thread comments and PR review-thread comments share far more register conventions with each other than either shares with a mailing-list email or a JIRA status-transition comment).
- Reported inter-rater agreement: mean Cohen's kappa 0.65 (TBDF, range 0.43-0.91) / 0.85 (justification codes), 2-author annotation, 20% double-coded
- Caveat: Same 2-rater, sub-0.667-floor caveat as ferreira_lkml; see that entry's note on D23's explicit gating decision for personal_attack/hostility/sarcasm.
- Caveat: Locked-issue selection bias: every thread was locked by a maintainer as "too heated," which skews the sampled threads toward already-escalated discussions relative to GitHub issues generally.
- Caveat: Same tbdf_categories rollup caveat as ferreira_lkml.

| Our label | Strength | Gate role | Best-F1 threshold | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) | Sample prevalence | Population prevalence | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `acknowledgment` | partial | informative | 0.05 | 0.016 [0.007, 0.027] | 0.769 [0.500, 1.000] | 0.032 [0.013, 0.053] | 13/2000 = 0.006 | 28/5115 = 0.005 | Same caveat as ferreira_lkml's acknowledgment mapping. appreciation_excitement does not actually occur in this dataset's GitHub civil-category set (it uses dissatisfaction/expectation instead where LKML uses appreciation_excitement/hope_to_get_feedback) -- kept in positive_values for documentation parity with ferreira_lkml; it simply never matches here. |
| `any_incivility_rollup` | n/a (synthetic rollup) | informative | 0.45 | 0.328 [0.280, 0.378] | 0.654 [0.585, 0.721] | 0.437 [0.382, 0.489] | 182/2000 = 0.091 | n/a | Same rollup as ferreira_lkml's any_incivility_rollup, GitHub-locked-issues side. |
| `dismissiveness` | partial | informative | 0.4 | 0.100 [0.046, 0.162] | 0.263 [0.139, 0.414] | 0.145 [0.071, 0.222] | 38/2000 = 0.019 | 87/5115 = 0.017 | Same caveat as ferreira_lkml's dismissiveness mapping. |
| `hostility` | strong | gating | 0.75 | 0.271 [0.184, 0.361] | 0.299 [0.205, 0.400] | 0.284 [0.198, 0.370] | 87/2000 = 0.043 | 243/5115 = 0.048 | Same TBDF scheme as ferreira_lkml, GitHub-locked-issues side. |
| `personal_attack` | strong | gating | 0.35 | 0.185 [0.118, 0.252] | 0.458 [0.318, 0.592] | 0.263 [0.174, 0.348] | 48/2000 = 0.024 | 133/5115 = 0.026 | Same TBDF scheme as ferreira_lkml, GitHub-locked-issues side. |
| `sarcasm` | strong | gating | 0.6 | 0.279 [0.187, 0.380] | 0.316 [0.205, 0.425] | 0.296 [0.200, 0.394] | 76/2000 = 0.038 | 203/5115 = 0.040 | Same TBDF scheme as ferreira_lkml, GitHub-locked-issues side. |
| `technical_disagreement` | partial | informative | 0.05 | 0.836 [0.817, 0.855] | 0.720 [0.697, 0.741] | 0.774 [0.757, 0.790] | 1606/1990 = 0.807 | 4398/5115 = 0.860 | Same caveat as ferreira_lkml's technical_disagreement mapping. |

Value at the §6.4 gate threshold: not applicable -- no label has a calibrated threshold yet (see "Thresholds" above).

### Separation by source category

Mean Jev probability of each of our 4 negative labels (`personal_attack`/`hostility`/`dismissiveness`/`sarcasm`), grouped by this dataset's own fine-grained category -- independent of any of the label mappings above. An item belonging to more than one category (e.g. a Ferreira quotation coded with two TBDF categories) is counted under each.

**Reading this table for Ferreira's TBDF scheme**: Ferreira's own inter-rater agreement was measured on a milder, *sentence-level* construct -- a coder flagged one quoted sentence within a message, not the message's overall tone. This benchmark's labels are message-level and calibrated to a broader bar. The two constructs disagree in both directions: Ferreira sometimes codes a single mild sentence ("Did you actually test this?" -> mocking) that reads as unremarkable at message level, while Jev sometimes flags a terse, fully uncoded message ("No. Just no.") that Ferreira's coders simply never marked. If Jev's mean negative-label probability nonetheless separates TBDF-uncivil-coded messages from civil/uncoded ones clearly (well above vs. well below the item-level precision/recall table's thresholds), that is evidence the classifier is picking up on real signal even where item-level F1 looks modest -- the F1 numbers above are a **lower bound on agreement**, not proof of a classifier error, because a meaningful share of the disagreement is definitional (which sentences vs. which messages, and how mild counts) rather than the classifier misreading the same construct Ferreira's coders used.

| Category | n | mean personal_attack | mean hostility | mean dismissiveness | mean sarcasm |
|---|---|---|---|---|---|
| (none coded) | 1651 | 0.060 | 0.127 | 0.072 | 0.122 |
| bitter_frustration | 79 | 0.204 | 0.439 | 0.351 | 0.182 |
| mocking | 64 | 0.277 | 0.445 | 0.326 | 0.402 |
| name_calling | 48 | 0.331 | 0.461 | 0.366 | 0.246 |
| dissatisfaction | 46 | 0.100 | 0.307 | 0.249 | 0.168 |
| expectation | 39 | 0.025 | 0.084 | 0.091 | 0.098 |
| sadness | 32 | 0.121 | 0.226 | 0.219 | 0.117 |
| considerateness | 31 | 0.059 | 0.062 | 0.164 | 0.135 |
| sincere_apologies | 24 | 0.081 | 0.100 | 0.132 | 0.127 |
| commanding | 20 | 0.182 | 0.316 | 0.328 | 0.154 |
| friendly_joke | 20 | 0.175 | 0.182 | 0.296 | 0.434 |
| impatience | 19 | 0.194 | 0.425 | 0.209 | 0.195 |
| irony | 17 | 0.256 | 0.473 | 0.352 | 0.481 |
| humility | 13 | 0.075 | 0.073 | 0.101 | 0.165 |
| vulgarity | 9 | 0.254 | 0.808 | 0.588 | 0.156 |
| oppression | 5 | 0.102 | 0.246 | 0.278 | 0.222 |
| threat | 5 | 0.314 | 0.628 | 0.396 | 0.278 |

## Ferreira, Cheng & Adams -- LKML incivility ("Shut the f**k up", 2021)

- Citation: Ferreira, Cheng & Adams, "The 'Shut the f**k up' Phenomenon: Characterizing Incivility in Open Source Code Review Discussions", PACMHCI 5(CSCW2), 2021, arXiv:2108.09905. Dataset republished as Ferreira, Rafiq & Cheng, "Incivility Detection in Open Source Code Review and Issue Discussions", Journal of Systems and Software, 2024, figshare DOI 10.6084/m9.figshare.24603237.v1.
- License: CC BY 4.0 (figshare API license.name, verified live 2026-09-27; note the GitHub repo iferreiradev/incivility_detection separately reports GPL-3.0 for the same data -- the figshare copy's CC BY 4.0 is used here, per the research doc's own resolution of that conflict)
- Classifier source venue used: `mailing_list` -- The Linux Kernel Mailing List is a mailing list -- a literal, not merely closest-analog, match to questions_v1.yaml's message.source enum.
- Reported inter-rater agreement: mean Cohen's kappa 0.62 (range 0.42-0.96), 2-author annotation, only partially double-coded
- Caveat: Only 2 raters (this project's own gate wants >=3), and per-code kappa as low as 0.42 -- below this project's own 0.667 Krippendorff's-alpha usable floor (COMMUNITY-HEALTH.md §6.3). D23 nonetheless names personal_attack/hostility/sarcasm from this dataset as this benchmark's strong, gating mappings; see label_mapping_v1.yaml's notes on each.
- Caveat: Ground truth is built from sentence-level TBDF (Tone-Bearing Discussion Feature) quotations rolled up to the whole message (raw_labels.tbdf_categories); a message positive for one category may not be positive for the whole message's dominant tone.
- Caveat: Selection bias toward rejected-patch threads (the corpus was sampled from contentious LKML threads specifically).

| Our label | Strength | Gate role | Best-F1 threshold | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) | Sample prevalence | Population prevalence | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `acknowledgment` | partial | informative | 0.65 | 0.556 [0.364, 0.737] | 0.366 [0.219, 0.511] | 0.441 [0.277, 0.575] | 41/1495 = 0.027 | 41/1495 = 0.027 | TBDF humility/appreciation_excitement are positive-affect categories adjacent to, but broader than, our acknowledgment definition (recognizing a specific point/correction/contribution). |
| `any_incivility_rollup` | n/a (synthetic rollup) | informative | 0.4 | 0.496 [0.414, 0.590] | 0.509 [0.419, 0.600] | 0.502 [0.424, 0.582] | 112/1495 = 0.075 | n/a | Not one of COMMUNITY-HEALTH.md's 12 labels -- this project's own rollup (orchestrator review of issue #89), added to answer "does Jev separate Ferreira-uncivil from Ferreira-civil at all" independent of which of our 4 negative labels the argument runs through. Scored against max(P( personal_attack), P(hostility), P(dismissiveness), P(sarcasm)) vs. whether the message carries ANY of Ferreira's 7 uncivil TBDF categories (not one of the 9 civil categories, and not "(none coded)"). Ferreira's TBDF scheme codes incivility per sentence-level quotation, a milder and narrower construct than our message-level labels -- see the "Separation by source category" section below: Jev's mean negative-label probability separates coded-uncivil from coded-civil/uncoded messages clearly even where this rollup's item-level F1 looks modest, because many of Ferreira's uncoded "civil" messages (e.g. a terse "No. Just no.") still read as dismissive/hostile under our broader definitions, and some of Ferreira's coded sentence-level incivility ("Did you actually test this?" -> mocking) is milder than what our labels are calibrated to flag. Item-level F1 against this dataset is therefore a lower bound on agreement, not proof of a classifier error. |
| `dismissiveness` | partial | informative | 0.15 | 0.164 [0.083, 0.250] | 0.286 [0.150, 0.422] | 0.209 [0.106, 0.301] | 42/1495 = 0.028 | 42/1495 = 0.028 | TBDF impatience/commanding are adjacent to dismissiveness (declining to engage with substance) but are not identical to it -- impatience can co-occur with an actual argument, which our dismissiveness definition excludes. |
| `hostility` | strong | gating | 0.4 | 0.493 [0.393, 0.612] | 0.521 [0.418, 0.638] | 0.507 [0.420, 0.604] | 71/1495 = 0.047 | 71/1495 = 0.047 | TBDF bitter_frustration/vulgarity/threat -- anger, aggression, or a threat directed at the discussion or a participant. |
| `personal_attack` | strong | gating | 0.15 | 0.511 [0.349, 0.652] | 0.451 [0.314, 0.589] | 0.479 [0.342, 0.593] | 51/1495 = 0.034 | 51/1495 = 0.034 | TBDF "name_calling" (identity attacks/name calling) -- the closest and most literal SE-domain match to our personal_attack definition found in this benchmark's shortlist. |
| `sarcasm` | strong | gating | 0.5 | 0.297 [0.152, 0.457] | 0.275 [0.140, 0.422] | 0.286 [0.149, 0.422] | 40/1495 = 0.027 | 40/1495 = 0.027 | TBDF irony/mocking -- the best sarcasm match in this benchmark's shortlist. |
| `technical_disagreement` | partial | informative | 0.05 | 0.879 [0.860, 0.897] | 0.818 [0.797, 0.839] | 0.847 [0.832, 0.862] | 1327/1495 = 0.888 | 1327/1495 = 0.888 | This dataset's "technical" vs. "not_technical" is a topic classification, not a stance label -- it says the message is about a technical matter, not that it disputes a claim on its merits. Partial and topic-general only. |

Value at the §6.4 gate threshold: not applicable -- no label has a calibrated threshold yet (see "Thresholds" above).

### Separation by source category

Mean Jev probability of each of our 4 negative labels (`personal_attack`/`hostility`/`dismissiveness`/`sarcasm`), grouped by this dataset's own fine-grained category -- independent of any of the label mappings above. An item belonging to more than one category (e.g. a Ferreira quotation coded with two TBDF categories) is counted under each.

**Reading this table for Ferreira's TBDF scheme**: Ferreira's own inter-rater agreement was measured on a milder, *sentence-level* construct -- a coder flagged one quoted sentence within a message, not the message's overall tone. This benchmark's labels are message-level and calibrated to a broader bar. The two constructs disagree in both directions: Ferreira sometimes codes a single mild sentence ("Did you actually test this?" -> mocking) that reads as unremarkable at message level, while Jev sometimes flags a terse, fully uncoded message ("No. Just no.") that Ferreira's coders simply never marked. If Jev's mean negative-label probability nonetheless separates TBDF-uncivil-coded messages from civil/uncoded ones clearly (well above vs. well below the item-level precision/recall table's thresholds), that is evidence the classifier is picking up on real signal even where item-level F1 looks modest -- the F1 numbers above are a **lower bound on agreement**, not proof of a classifier error, because a meaningful share of the disagreement is definitional (which sentences vs. which messages, and how mild counts) rather than the classifier misreading the same construct Ferreira's coders used.

| Category | n | mean personal_attack | mean hostility | mean dismissiveness | mean sarcasm |
|---|---|---|---|---|---|
| (none coded) | 1327 | 0.031 | 0.067 | 0.054 | 0.082 |
| bitter_frustration | 64 | 0.154 | 0.430 | 0.093 | 0.264 |
| name_calling | 51 | 0.202 | 0.420 | 0.108 | 0.292 |
| impatience | 35 | 0.149 | 0.450 | 0.140 | 0.240 |
| mocking | 35 | 0.167 | 0.431 | 0.124 | 0.294 |
| humility | 31 | 0.042 | 0.082 | 0.048 | 0.127 |
| considerateness | 17 | 0.057 | 0.125 | 0.056 | 0.092 |
| appreciation_excitement | 16 | 0.030 | 0.038 | 0.052 | 0.096 |
| sincere_apologies | 13 | 0.040 | 0.041 | 0.071 | 0.068 |
| threat | 8 | 0.271 | 0.531 | 0.050 | 0.253 |
| vulgarity | 8 | 0.104 | 0.654 | 0.099 | 0.323 |
| commanding | 7 | 0.101 | 0.234 | 0.149 | 0.063 |
| sadness | 7 | 0.124 | 0.551 | 0.049 | 0.259 |
| irony | 6 | 0.150 | 0.358 | 0.090 | 0.502 |
| friendly_joke | 5 | 0.036 | 0.060 | 0.048 | 0.174 |
| hope_to_get_feedback | 4 | 0.133 | 0.237 | 0.080 | 0.095 |
| oppression | 3 | 0.053 | 0.333 | 0.050 | 0.253 |

## TalkDown (Wang & Potts, 2019)

- Citation: Wang & Potts, "TalkDown: A Corpus for Detecting Condescension in the Alt-Right", EMNLP-IJCNLP 2019. github.com/zijwang/talkdown.
- License: AGPL-3.0 (GitHub API license.spdx_id, verified live 2026-09-27; repo-level, no separate data-only license file)
- Classifier source venue used: `mailing_list` -- Reddit comment/reply pairs are free-form, asynchronous, threaded prose discussion with no code diff anchoring -- closer in register to a mailing-list thread than to a JIRA status-transition comment or a diff-anchored GitHub PR review comment.
- Reported inter-rater agreement: Fleiss' kappa 0.593, MTurk crowd workers, EM-aggregated labels
- Caveat: Condescension (TalkDown's own construct) is mapped only to dismissiveness, and only as a partial match -- TalkDown's definition also covers insincere praise, which is not what our dismissiveness label describes. It is NOT mapped to gatekeeping: condescension-as-flagged-by-a-reply is adjacent to standing-denial but is not the same claim (our gatekeeping definition is specifically about asserting a participant lacks standing to contribute), so this benchmark does not credit or debit gatekeeping from this dataset at all.
- Caveat: Not gating: a single Fleiss' kappa of 0.593 on a differently-defined construct is below this project's 0.667 usable floor, and the mapping itself is only partial.

| Our label | Strength | Gate role | Best-F1 threshold | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) | Sample prevalence | Population prevalence | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `dismissiveness` | partial | informative | 0.05 | 0.726 [0.711, 0.741] | 0.804 [0.789, 0.818] | 0.763 [0.751, 0.774] | 3255/4992 = 0.652 | 3255/4992 = 0.652 | TalkDown's condescension label is broader than dismissiveness (it also covers insincere praise) and is applied to the REPLY's accusation, not directly to whether the original speaker declined to engage. Not mapped to gatekeeping at all: condescension-as-flagged-by-a-reply is adjacent to standing-denial but is not the same claim as our gatekeeping definition (asserting a participant lacks standing to contribute) -- this benchmark credits/debits gatekeeping from no dataset in this shortlist. |

Value at the §6.4 gate threshold: not applicable -- no label has a calibrated threshold yet (see "Thresholds" above).

### Separation by source category

Mean Jev probability of each of our 4 negative labels (`personal_attack`/`hostility`/`dismissiveness`/`sarcasm`), grouped by this dataset's own fine-grained category -- independent of any of the label mappings above. An item belonging to more than one category (e.g. a Ferreira quotation coded with two TBDF categories) is counted under each.

| Category | n | mean personal_attack | mean hostility | mean dismissiveness | mean sarcasm |
|---|---|---|---|---|---|
| condescending | 3255 | 0.734 | 0.826 | 0.157 | 0.495 |
| not_condescending | 1737 | 0.454 | 0.541 | 0.083 | 0.383 |

## ToxiCR (Sarker, Turzo, Dong & Bosu, 2023)

- Citation: Sarker, Turzo, Dong & Bosu, "Automated Identification of Toxic Code Reviews Using ToxiCR", ACM TOSEM 32(1), 2023, arXiv:2202.13056. github.com/WSU-SEAL/ToxiCR.
- License: GPL-3.0 (GitHub API license.spdx_id, verified live 2026-09-27; a software-copyleft concern for redistributing the tool/data, not a bar on publishing this benchmark's derived aggregate results)
- Classifier source venue used: `github_pr_comment` -- ToxiCR's items are Gerrit/OpenStack code-review comments -- not literally GitHub, but the same short, technical, diff-anchored review-comment register questions_v1.yaml's github_pr_comment venue describes; closer to that than to a mailing-list email or a JIRA comment.
- Reported inter-rater agreement: Cohen's kappa 0.727 (reused Sarker/Turzo/Bosu 2020 labels) / 0.92 (newly added labels), 2-author annotation
- Caveat: "is_toxic" is a single binary bit merged across 9 underlying rules (profanity, insults, identity attacks, threats, sexual references, flirtation, ...) -- broader than any one of our labels; mapped to both personal_attack and hostility as an equally imprecise partial proxy for each, plus this project's own "any_negative_rollup" (see label_mapping_v1.yaml), documented there as ours, not ToxiCR's own construct.
- Caveat: No parent/context text at all -- every item is classified with parent.text=null.
- Caveat: Class imbalance ~80/20 non-toxic/toxic; source projects (Android, Chromium, LibreOffice, OpenStack) are large, well-governed ones that may under-represent some toxicity styles.

| Our label | Strength | Gate role | Best-F1 threshold | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) | Sample prevalence | Population prevalence | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `any_negative_rollup` | n/a (synthetic rollup) | informative | 0.1 | 0.761 [0.741, 0.782] | 0.840 [0.822, 0.859] | 0.799 [0.784, 0.814] | 1500/3000 = 0.500 | n/a | Not one of COMMUNITY-HEALTH.md's 12 labels -- this project's own rollup (issue #89: "map it to hostility or to an 'any_negative' rollup (the max of the negative labels). If you add a rollup, document it as ours."), added specifically because ToxiCR's single "is_toxic" bit is broader than either personal_attack or hostility alone. Scored against max(P(personal_attack), P(hostility)) from the same classification record. Always informative, never gating. |
| `hostility` | partial | informative | 0.1 | 0.771 [0.750, 0.790] | 0.815 [0.795, 0.836] | 0.793 [0.777, 0.808] | 1500/3000 = 0.500 | 3757/19651 = 0.191 | Same broad, merged proxy as personal_attack above, for the same raw bit. |
| `personal_attack` | partial | informative | 0.05 | 0.749 [0.726, 0.773] | 0.554 [0.529, 0.582] | 0.637 [0.617, 0.659] | 1500/3000 = 0.500 | 3757/19651 = 0.191 | ToxiCR's single "is_toxic" bit merges 9 underlying rules (profanity, insults, identity attacks, threats, sexual references, flirtation, ...) into one undifferentiated flag -- a broad, imprecise proxy for personal_attack specifically. |

Value at the §6.4 gate threshold: not applicable -- no label has a calibrated threshold yet (see "Thresholds" above).

### Separation by source category

Mean Jev probability of each of our 4 negative labels (`personal_attack`/`hostility`/`dismissiveness`/`sarcasm`), grouped by this dataset's own fine-grained category -- independent of any of the label mappings above. An item belonging to more than one category (e.g. a Ferreira quotation coded with two TBDF categories) is counted under each.

| Category | n | mean personal_attack | mean hostility | mean dismissiveness | mean sarcasm |
|---|---|---|---|---|---|
| not_toxic | 1500 | 0.050 | 0.099 | 0.119 | 0.098 |
| toxic | 1500 | 0.116 | 0.352 | 0.160 | 0.208 |

## Wikipedia Personal Attacks / Aggression (Wulczyn, Thain & Dixon, 2017)

- Citation: Wulczyn, Thain & Dixon, "Ex Machina: Personal Attacks Seen at Scale", WWW 2017, arXiv:1610.08914. figshare DOI 10.6084/m9.figshare.4054689.
- License: CC0 (figshare API license.name, verified live 2026-09-27; the paper itself is published under CC BY 4.0)
- Classifier source venue used: `mailing_list` -- Wikipedia talk-page comments are free-form, asynchronous, threaded prose discussion with no code diff anchoring -- closer in register to a mailing-list thread than to a JIRA comment or a diff-anchored GitHub PR review comment (this dataset also carries no parent/reply text at all, so classification runs with parent.text=null regardless of venue choice).
- Reported inter-rater agreement: Krippendorff's alpha 0.45 (paper's own figure) -- below this project's 0.667 usable floor (COMMUNITY-HEALTH.md §6.3)
- Caveat: Informative only, never gating (D23, this dataset by name): alpha=0.45 is below the usable floor, so this benchmark's personal_attack results here are reported as precision-floor/volume calibration, not as evidence the label clears its §6.4 gate.
- Caveat: Ground truth is majority vote (mean(attack) > 0.5) across ~10 crowd workers per comment, per the paper's own convention.
- Caveat: No parent/context text; each comment is scored standalone.

| Our label | Strength | Gate role | Best-F1 threshold | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) | Sample prevalence | Population prevalence | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `personal_attack` | strong | informative | 0.45 | 0.812 [0.797, 0.827] | 0.918 [0.908, 0.929] | 0.862 [0.852, 0.871] | 2500/5000 = 0.500 | 13590/115864 = 0.117 | Literal, strong match to our personal_attack construct, but this dataset's own Krippendorff's alpha (0.45) is below this project's 0.667 usable floor (COMMUNITY-HEALTH.md §6.3) -- reported here as informative precision-floor/volume calibration only, never as a §6.4 gate result (D23 names this dataset by name for this treatment). |

Value at the §6.4 gate threshold: not applicable -- no label has a calibrated threshold yet (see "Thresholds" above).

### Separation by source category

Mean Jev probability of each of our 4 negative labels (`personal_attack`/`hostility`/`dismissiveness`/`sarcasm`), grouped by this dataset's own fine-grained category -- independent of any of the label mappings above. An item belonging to more than one category (e.g. a Ferreira quotation coded with two TBDF categories) is counted under each.

| Category | n | mean personal_attack | mean hostility | mean dismissiveness | mean sarcasm |
|---|---|---|---|---|---|
| attack | 2500 | 0.853 | 0.884 | 0.221 | 0.285 |
| no_attack | 2500 | 0.233 | 0.334 | 0.097 | 0.227 |

