# Public benchmark: TypeSafe Jev vs. public human-labeled datasets

DECISIONS.md D22, D23; COMMUNITY-HEALTH.md §1, §6; issue #89. Primary classifier benchmark: `project-health benchmark-public` scores the pinned TypeSafe Jev classifier (D17) against public, human-labeled datasets whose own label schemes are mapped onto this project's 12 message-level labels (`label_mapping_v1.yaml`, `docs/plans/2026-09-27-public-benchmark-datasets.md`).

**Aggregate only.** No item text, no item/message ids, and no per-item detail appear anywhere below -- per COMMUNITY-HEALTH.md §7's "quotations: default none" and D23's "only aggregate results are published." Dataset text is fetched into a local cache directory at run time and is never committed to this repo or redistributed.

- Generated at: 2026-09-27T17:08:02.289651+00:00

## Cost and latency

- Calls made: 16148 (cache hits: 339)
- Input / output tokens: 49336143 / 4053148
- Estimated cost (USD): 2.072118006
- Elapsed seconds: 396.6574514580425
- Mean latency per call (seconds): 0.02456387487354734
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

| Dataset | Citation | License | Sample n | Population n |
|---|---|---|---|---|
| Ferreira, Adams & Cheng -- GitHub locked issues ("How heated is it?", 2022) | Ferreira, Adams & Cheng, "How heated is it? Understanding GitHub locked issues", MSR 2022, arXiv:2204.00155. Dataset republished as Ferreira, Rafiq & Cheng, "Incivility Detection in Open Source Code Review and Issue Discussions", Journal of Systems and Software, 2024, figshare DOI 10.6084/m9.figshare.24603237.v1. | CC BY 4.0 (figshare API license.name, verified live 2026-09-27; see ferreira_lkml's note on the GPL-3.0-vs-CC-BY-4.0 conflict between the GitHub repo and the figshare copy of this same data) | 2000 | 5115 |
| Ferreira, Cheng & Adams -- LKML incivility ("Shut the f**k up", 2021) | Ferreira, Cheng & Adams, "The 'Shut the f**k up' Phenomenon: Characterizing Incivility in Open Source Code Review Discussions", PACMHCI 5(CSCW2), 2021, arXiv:2108.09905. Dataset republished as Ferreira, Rafiq & Cheng, "Incivility Detection in Open Source Code Review and Issue Discussions", Journal of Systems and Software, 2024, figshare DOI 10.6084/m9.figshare.24603237.v1. | CC BY 4.0 (figshare API license.name, verified live 2026-09-27; note the GitHub repo iferreiradev/incivility_detection separately reports GPL-3.0 for the same data -- the figshare copy's CC BY 4.0 is used here, per the research doc's own resolution of that conflict) | 1495 | 1495 |
| TalkDown (Wang & Potts, 2019) | Wang & Potts, "TalkDown: A Corpus for Detecting Condescension in the Alt-Right", EMNLP-IJCNLP 2019. github.com/zijwang/talkdown. | AGPL-3.0 (GitHub API license.spdx_id, verified live 2026-09-27; repo-level, no separate data-only license file) | 4992 | 4992 |
| ToxiCR (Sarker, Turzo, Dong & Bosu, 2023) | Sarker, Turzo, Dong & Bosu, "Automated Identification of Toxic Code Reviews Using ToxiCR", ACM TOSEM 32(1), 2023, arXiv:2202.13056. github.com/WSU-SEAL/ToxiCR. | GPL-3.0 (GitHub API license.spdx_id, verified live 2026-09-27; a software-copyleft concern for redistributing the tool/data, not a bar on publishing this benchmark's derived aggregate results) | 3000 | 19651 |
| Wikipedia Personal Attacks / Aggression (Wulczyn, Thain & Dixon, 2017) | Wulczyn, Thain & Dixon, "Ex Machina: Personal Attacks Seen at Scale", WWW 2017, arXiv:1610.08914. figshare DOI 10.6084/m9.figshare.4054689. | CC0 (figshare API license.name, verified live 2026-09-27; the paper itself is published under CC BY 4.0) | 5000 | 115864 |

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
| `acknowledgment` | partial | informative | 0.05 | 0.017 [0.007, 0.028] | 0.769 [0.500, 1.000] | 0.033 [0.013, 0.054] | 13/1953 = 0.007 | 28/5115 = 0.005 | Same caveat as ferreira_lkml's acknowledgment mapping. appreciation_excitement does not actually occur in this dataset's GitHub civil-category set (it uses dissatisfaction/expectation instead where LKML uses appreciation_excitement/hope_to_get_feedback) -- kept in positive_values for documentation parity with ferreira_lkml; it simply never matches here. |
| `dismissiveness` | partial | informative | 0.4 | 0.100 [0.044, 0.169] | 0.263 [0.129, 0.429] | 0.145 [0.067, 0.234] | 38/1953 = 0.019 | 87/5115 = 0.017 | Same caveat as ferreira_lkml's dismissiveness mapping. |
| `hostility` | strong | gating | 0.75 | 0.271 [0.178, 0.366] | 0.299 [0.202, 0.402] | 0.284 [0.193, 0.371] | 87/1953 = 0.045 | 243/5115 = 0.048 | Same TBDF scheme as ferreira_lkml, GitHub-locked-issues side. |
| `personal_attack` | strong | gating | 0.35 | 0.185 [0.118, 0.254] | 0.458 [0.319, 0.600] | 0.263 [0.173, 0.344] | 48/1953 = 0.025 | 133/5115 = 0.026 | Same TBDF scheme as ferreira_lkml, GitHub-locked-issues side. |
| `sarcasm` | strong | gating | 0.6 | 0.279 [0.186, 0.372] | 0.316 [0.210, 0.420] | 0.296 [0.203, 0.381] | 76/1953 = 0.039 | 203/5115 = 0.040 | Same TBDF scheme as ferreira_lkml, GitHub-locked-issues side. |
| `technical_disagreement` | partial | informative | 0.05 | 0.835 [0.816, 0.854] | 0.738 [0.717, 0.758] | 0.783 [0.767, 0.799] | 1559/1943 = 0.802 | 4398/5115 = 0.860 | Same caveat as ferreira_lkml's technical_disagreement mapping. |

Value at the §6.4 gate threshold: not applicable -- no label has a calibrated threshold yet (see "Thresholds" above).

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
| `acknowledgment` | partial | informative | 0.65 | 0.556 [0.360, 0.750] | 0.366 [0.216, 0.514] | 0.441 [0.280, 0.583] | 41/1472 = 0.028 | 41/1495 = 0.027 | TBDF humility/appreciation_excitement are positive-affect categories adjacent to, but broader than, our acknowledgment definition (recognizing a specific point/correction/contribution). |
| `dismissiveness` | partial | informative | 0.15 | 0.164 [0.085, 0.250] | 0.286 [0.152, 0.439] | 0.209 [0.111, 0.305] | 42/1472 = 0.029 | 42/1495 = 0.028 | TBDF impatience/commanding are adjacent to dismissiveness (declining to engage with substance) but are not identical to it -- impatience can co-occur with an actual argument, which our dismissiveness definition excludes. |
| `hostility` | strong | gating | 0.4 | 0.493 [0.383, 0.600] | 0.521 [0.405, 0.631] | 0.507 [0.408, 0.597] | 71/1472 = 0.048 | 71/1495 = 0.047 | TBDF bitter_frustration/vulgarity/threat -- anger, aggression, or a threat directed at the discussion or a participant. |
| `personal_attack` | strong | gating | 0.15 | 0.511 [0.366, 0.667] | 0.451 [0.317, 0.583] | 0.479 [0.342, 0.593] | 51/1472 = 0.035 | 51/1495 = 0.034 | TBDF "name_calling" (identity attacks/name calling) -- the closest and most literal SE-domain match to our personal_attack definition found in this benchmark's shortlist. |
| `sarcasm` | strong | gating | 0.5 | 0.297 [0.143, 0.468] | 0.275 [0.135, 0.419] | 0.286 [0.143, 0.422] | 40/1472 = 0.027 | 40/1495 = 0.027 | TBDF irony/mocking -- the best sarcasm match in this benchmark's shortlist. |
| `technical_disagreement` | partial | informative | 0.05 | 0.877 [0.857, 0.896] | 0.820 [0.798, 0.842] | 0.847 [0.831, 0.863] | 1304/1472 = 0.886 | 1327/1495 = 0.888 | This dataset's "technical" vs. "not_technical" is a topic classification, not a stance label -- it says the message is about a technical matter, not that it disputes a claim on its merits. Partial and topic-general only. |

Value at the §6.4 gate threshold: not applicable -- no label has a calibrated threshold yet (see "Thresholds" above).

## TalkDown (Wang & Potts, 2019)

- Citation: Wang & Potts, "TalkDown: A Corpus for Detecting Condescension in the Alt-Right", EMNLP-IJCNLP 2019. github.com/zijwang/talkdown.
- License: AGPL-3.0 (GitHub API license.spdx_id, verified live 2026-09-27; repo-level, no separate data-only license file)
- Classifier source venue used: `mailing_list` -- Reddit comment/reply pairs are free-form, asynchronous, threaded prose discussion with no code diff anchoring -- closer in register to a mailing-list thread than to a JIRA status-transition comment or a diff-anchored GitHub PR review comment.
- Reported inter-rater agreement: Fleiss' kappa 0.593, MTurk crowd workers, EM-aggregated labels
- Caveat: Condescension (TalkDown's own construct) is mapped only to dismissiveness, and only as a partial match -- TalkDown's definition also covers insincere praise, which is not what our dismissiveness label describes. It is NOT mapped to gatekeeping: condescension-as-flagged-by-a-reply is adjacent to standing-denial but is not the same claim (our gatekeeping definition is specifically about asserting a participant lacks standing to contribute), so this benchmark does not credit or debit gatekeeping from this dataset at all.
- Caveat: Not gating: a single Fleiss' kappa of 0.593 on a differently-defined construct is below this project's 0.667 usable floor, and the mapping itself is only partial.

| Our label | Strength | Gate role | Best-F1 threshold | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) | Sample prevalence | Population prevalence | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `dismissiveness` | partial | informative | 0.05 | 0.726 [0.712, 0.741] | 0.804 [0.790, 0.818] | 0.763 [0.752, 0.775] | 3255/4992 = 0.652 | 3255/4992 = 0.652 | TalkDown's condescension label is broader than dismissiveness (it also covers insincere praise) and is applied to the REPLY's accusation, not directly to whether the original speaker declined to engage. Not mapped to gatekeeping at all: condescension-as-flagged-by-a-reply is adjacent to standing-denial but is not the same claim as our gatekeeping definition (asserting a participant lacks standing to contribute) -- this benchmark credits/debits gatekeeping from no dataset in this shortlist. |

Value at the §6.4 gate threshold: not applicable -- no label has a calibrated threshold yet (see "Thresholds" above).

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
| `any_negative_rollup` | n/a (synthetic rollup) | informative | 0.1 | 0.740 [0.717, 0.763] | 0.839 [0.818, 0.859] | 0.786 [0.769, 0.804] | 1290/2734 = 0.472 | n/a | Not one of COMMUNITY-HEALTH.md's 12 labels -- this project's own rollup (issue #89: "map it to hostility or to an 'any_negative' rollup (the max of the negative labels). If you add a rollup, document it as ours."), added specifically because ToxiCR's single "is_toxic" bit is broader than either personal_attack or hostility alone. Scored against max(P(personal_attack), P(hostility)) from the same classification record. Always informative, never gating. |
| `hostility` | partial | informative | 0.1 | 0.750 [0.729, 0.772] | 0.816 [0.795, 0.834] | 0.782 [0.764, 0.798] | 1290/2734 = 0.472 | 3757/19651 = 0.191 | Same broad, merged proxy as personal_attack above, for the same raw bit. |
| `personal_attack` | partial | informative | 0.05 | 0.730 [0.702, 0.760] | 0.552 [0.525, 0.580] | 0.629 [0.605, 0.654] | 1290/2734 = 0.472 | 3757/19651 = 0.191 | ToxiCR's single "is_toxic" bit merges 9 underlying rules (profanity, insults, identity attacks, threats, sexual references, flirtation, ...) into one undifferentiated flag -- a broad, imprecise proxy for personal_attack specifically. |

Value at the §6.4 gate threshold: not applicable -- no label has a calibrated threshold yet (see "Thresholds" above).

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
| `personal_attack` | strong | informative | 0.45 | 0.812 [0.797, 0.826] | 0.919 [0.907, 0.929] | 0.862 [0.851, 0.871] | 2497/4997 = 0.500 | 13590/115864 = 0.117 | Literal, strong match to our personal_attack construct, but this dataset's own Krippendorff's alpha (0.45) is below this project's 0.667 usable floor (COMMUNITY-HEALTH.md §6.3) -- reported here as informative precision-floor/volume calibration only, never as a §6.4 gate result (D23 names this dataset by name for this treatment). |

Value at the §6.4 gate threshold: not applicable -- no label has a calibrated threshold yet (see "Thresholds" above).

