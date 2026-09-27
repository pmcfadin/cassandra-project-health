# Public human-labeled datasets for benchmarking the Jev message classifier

Research pass for D22 (the frozen benchmark is a plain script comparing Jev's outputs
against human labels — no LLM at runtime). Goal: find large, publicly available,
**human-labeled** datasets that can supplement, or substitute for, a small
owner-labeled pilot when validating the 12 message-level labels and `tone_intensity`
defined in `docs/spec/COMMUNITY-HEALTH.md` §1.2 and encoded in
`src/project_health/classify/questions_v1.yaml`.

**Method.** Four parallel research passes fetched primary sources directly — GitHub
repos and their `LICENSE` files, arXiv/ACL Anthology PDFs, figshare/Zenodo/HuggingFace/
Kaggle dataset-card APIs, and OpenReview submission metadata — rather than relying on
memory or search snippets. Every license, size, and agreement figure below is quoted
from a source actually fetched during this research pass, unless marked **UNVERIFIED**.
Two corrections surfaced against the candidate list in the task brief: Raman et al. 2020
was published in the ICSE **NIER** track (not "SEIS"), and no paper titled "Heated
debates" or "Shut up and keep talking" exists — the actual Ferreira et al. LKML paper is
titled *"The 'Shut the f\*\*k up' Phenomenon."*

**Bottom line.** No public dataset was found that reproduces this project's exact
12-label + tone taxonomy, multi-rater (≥3) design, or Krippendorff's-alpha gate.
The strongest external sources cover `personal_attack`, `hostility`, and `sarcasm`
reasonably well; they are far weaker or silent on the six "argument" labels
(`technical_disagreement`, `constructive_counterargument`, `evidence_based_argument`,
`compromise_offer`, `acknowledgment`, `resolution_marker`) and on `gatekeeping`,
`status_authority_invocation`, and `tone_intensity` as this project defines them. Public
data is therefore best used to **augment and pressure-test** an owner/SE-expert-labeled
pilot on the negative/adversarial labels — not to replace the pilot for the argument
labels or the two authority-structure labels, which appear to have no public precedent
at all.

---

## 1. Summary table

Legend for "Access": **open** = direct download, no login/form; **gated** = login,
request form, or on-request-only; **dead** = the dataset's own canonical URL no longer
resolves (recovered via mirror/Wayback where noted).

| # | Dataset | Year | Domain | Size (items) | Annotators/item | Reported IAA | License (as verified) | Access |
|---|---|---|---|---|---|---|---|---|
| 1 | Wikipedia Personal Attacks / Aggression (Wulczyn et al.) | 2017 | Wikipedia talk pages | 115,737 comments | 10 crowd | Krippendorff's α = 0.45 | CC0 (data) / CC BY 4.0 (paper) | open |
| 2 | ConvoKit CGA-WIKI | 2018 | Wikipedia talk pages | 4,188 convs / 30,021 comments | 3 crowd + internal verify | UNVERIFIED | UNVERIFIED (no license on page) | open |
| 3 | ConvoKit CGA-CMV / CGA-CMV-Large | 2019/2024 | Reddit r/ChangeMyView | 6,842–19,578 convs | UNVERIFIED | UNVERIFIED | UNVERIFIED | open |
| 4 | WikiDisputes (De Kock & Vlachos) | 2021 | Wikipedia talk pages | 7,425 convs | n/a (administrative label) | n/a | UNVERIFIED (repo `license: null`) | open |
| 5 | WikiTactics (De Kock, Stafford & Vlachos) | 2022 | Wikipedia talk pages | 213 disputes / 3,865 utterances | 1 (main pass) | Cohen's κ 0.17→0.55, Pearson 0.68 (**pilot only**, not on released labels) | UNVERIFIED (repo `license: null`) | open |
| 6 | Stanford Politeness Corpus (Wiki + Stack Exchange) | 2013 | Wikipedia + Stack Exchange requests | 4,353 (Wiki) + 6,604 (SE) | 5 crowd (MTurk) | mean pairwise Pearson r ≈0.68 (Wiki) / 0.58 (SE) | CC BY 4.0 (quoted) | open |
| 7 | Jigsaw Toxic Comment Classification Challenge | 2017/18 | Wikipedia talk pages | ~223,000 (159,571 train) | 10 crowd | not found on page | CC0 (HF mirror) | open (HF) / gated (Kaggle) |
| 8 | Jigsaw Civil Comments (Unintended Bias) | 2019 | News-site comments | ~2,000,000 | ~crowd, exact count UNVERIFIED | not found in paper sections read | CC0 (quoted) | open (HF) / gated (Kaggle) |
| 9 | SARC (Self-Annotated Reddit Corpus) | 2018 | Reddit | ~1M+ (exact count UNVERIFIED) | 0 — self-tag `/s`, distant supervision | n/a (not human-rated) | MIT (code only; data license UNVERIFIED) | open |
| 10 | iSarcasm / iSarcasmEval | 2020/22 | Twitter | 4,484 (iSarcasm) | author self-label + crowd check | crowd-vs-author F=0.616 | UNVERIFIED ("research purposes" wording); iSarcasmEval = MIT | open (via ACL attachment; original repo dead) |
| 11 | IBM Debater Evidence Sentences family | 2015–2020 | Wikipedia sentences / crowd essays | 5,785 / 29,429 / 30,497 / 6.3k+ pairs (per sub-set) | crowd (Figure Eight) | not found | CC-BY-SA (quoted, archived page) | gated (request forms); canonical page now 404 |
| 12 | UKP Sentential Argument Mining Corpus | 2018 | Mixed web text, 8 topics | ~27,520 sentences | 2 expert + crowd (MACE) | Cohen's κ = 0.721 (2 experts) | UNVERIFIED (both official URLs unreachable) | UNVERIFIED |
| 13 | TalkDown | 2019 | Reddit comment/reply | 4,992 instances | crowd (MTurk), EM-aggregated | Fleiss' κ = 0.593 | AGPL-3.0 (repo-level; no separate data license) | open |
| 14 | DEBAGREEMENT | 2021 | Reddit (5 political/social subreddits) | 42,894 comment-reply pairs | ≥3 (type UNVERIFIED) | reported via Full/2-of-3/Challenge tiers, no single κ | CC BY 4.0 (quoted, OpenReview metadata) | dead canonical link (Scale AI); unverified mirrors |
| 15 | Internet Argument Corpus (IAC v2) | 2016 | Debate forums (4forums, ConvinceMe, CreateDebate) | 414,453 posts (4forums) total; 9,975 quote-response pairs annotated | crowd (MTurk) | not found in 2016 paper (may be in 2012 IAC 1.0 paper) | UNVERIFIED ("free for research use" only) | informal (Drive link + email) |
| 16 | Sarker, Turzo & Bosu 2020 benchmark | 2020 | Gerrit code review (Android/Chromium/LibreOffice) + Ethereum Gitter | 6,533 + 4,140 | 2 authors | Cohen's κ 0.727 / 0.781 | CC0 (quoted LICENSE) | open |
| 17 | ToxiCR | 2023 | Code review comments (Gerrit + OpenStack) | 19,651 (3,757 toxic) | 2 authors | κ 0.727 (reused) / 0.92 (new) | GPL-3.0 (quoted LICENSE) | open |
| 18 | Raman et al. 2020 ("Stress and burnout") | 2020 | GitHub issues | 1,597 comments / 309 + 193 issue threads | apparently 1 (undergrad REU) | none reported | MIT (quoted LICENSE) | open |
| 19 | Miller et al. 2022 ("Did you miss my comment or what?") | 2022 | GitHub issues | 100 toxic threads (no negatives) | authors; 3-rater subset only for binary screen | Cohen's κ 0.82 / Fleiss κ 0.72 (**binary screen only**, not categories) | none stated; on-request only | gated |
| 20 | Ferreira, Cheng & Adams — LKML incivility ("Shut the f\*\*k up") | 2021 | Linux Kernel Mailing List | 1,545 emails / 262 threads | 2 authors (partial double-code) | mean κ = 0.62 (range 0.42–0.96) | CC BY 4.0 (quoted, figshare) | open |
| 21 | Ferreira, Adams & Cheng — GitHub locked issues ("How heated is it?") | 2022 | GitHub locked issues | 205 issues / 5,511 comments | 2 authors (20% double-coded) | mean κ = 0.65 (TBDF) / 0.85 (justification) | CC BY 4.0 (quoted, figshare) | open |
| 22 | Ferreira, Rafiq & Cheng — combined incivility classifier data | 2024 | LKML + GitHub (reuses 20–21) | 276+117 (LKML) / 896+353 (GitHub) sentences | (inherited from 20–21) | (inherited) | **Conflict:** GPL-3.0 (GitHub repo) vs CC BY 4.0 (figshare copy) — use the figshare copy | open |
| 23 | Ehsani et al. 2024 GitHub incivility | 2024 | GitHub locked issues (213 projects) | 404 threads / 5,961 comments | 19 students, **1 per thread** (not multi-rater) | none (GPT-4 used for QA, not human IAA) | MIT (quoted LICENSE) | open |
| 24 | Ortu et al. JIRA emotions | 2016 | JIRA issue comments | ~4,000 issues | UNVERIFIED | UNVERIFIED | MIT (mirror repo) | open (via collab-uniba mirror) |
| 25 | Novielli/Calefato/Lanubile — EmoTxt (SO emotion) / Senti4SD (SO polarity) | 2018 | Stack Overflow | 4,800 (emotion) + 4,424 (polarity) | UNVERIFIED | UNVERIFIED | MIT (Senti4SD/EMTk); MSR18 emotion set has **no formal license**, citation-only "Fair Use" ask | open |

---

## 2. Per-dataset detail

Grouped by relevance tier. Each entry gives the mapping to our 12 labels + `tone_intensity`
as **strong / partial / none**, one line each, only where non-obvious from the summary
table above (labels omitted from an entry's mapping list are **none**).

### 2.1 Software-engineering / open-source communication (highest relevance)

**16–17. Sarker, Turzo & Bosu (2020) benchmark + ToxiCR (2023)** — WSU-SEAL, GitHub
`WSU-SEAL/toxicity-dataset` and `WSU-SEAL/ToxiCR`.
- Citations: Sarker, Turzo & Bosu, *A Benchmark Study of the Contemporary Toxicity
  Detectors on Software Engineering Interactions*, APSEC 2020, arXiv:2009.09331; Sarker,
  Turzo, Dong & Bosu, *Automated Identification of Toxic Code Reviews Using ToxiCR*,
  ACM TOSEM 32(1), 2023, arXiv:2202.13056.
- License quoted: 2020 set's `LICENSE` begins "Creative Commons Legal Code / CC0 1.0
  Universal"; ToxiCR's repo `LICENSE` is the full GPLv3 text. GPL's copyleft obligations
  are a software-copyleft concern, not a bar on publishing derived aggregate results.
- Label scheme: binary Toxic/Non-toxic against a 9-rule rubric (profanity, profane
  acronyms, insults, identity attacks, threats, sexual references, flirtation,
  self-deprecation explicitly excluded, everything else non-toxic).
- Mapping: `personal_attack` partial, `hostility` partial (insult/identity-attack/threat
  rules exist but are merged into one undifferentiated "toxic" bit alongside profanity
  and flirtation); all 6 argument labels, `dismissiveness`, `sarcasm`, `gatekeeping`,
  `status_authority_invocation`, `resolution_marker`, `tone_intensity` — none.
- Caveats: only 2 author-raters; message-level with **no parent/context text**; class
  imbalance ~80/20; four large, well-governed projects (Android/Chromium/LibreOffice/
  Ethereum), likely under-representing some toxicity styles.

**18. Raman et al. 2020, "Stress and Burnout in Open Source"** — ICSE-NIER 2020,
`CMUSTRUDEL/toxicity-detector` (MIT, quoted).
- 1,597 labeled GitHub comments (101 toxic, 6.3%) + 309/193 labeled issue threads,
  counted directly from the CSVs. Paper's stated "386 labeled threads" does not match
  the released files; the discrepancy is unexplained.
- Binary `toxicity` column, **no codebook and no inter-annotator agreement reported at
  all** — the paper contains no kappa/agreement statistic. Apparently single-annotator
  (first author, an undergraduate REU student).
- Mapping: `personal_attack`/`hostility` partial (one merged "toxic" bit); everything
  else none. `tone_intensity` partial only via ancillary machine-generated columns
  (`perspective_score`, `stanford_polite`, `polarity`, `subjectivity` — not human tone
  ratings).
- Caveat: positives sampled from GitHub-locked "too heated" issues plus comments
  containing "attitude" — a biased, not random, positive set.

**19. Miller et al. 2022, "Did you miss my comment or what?"** — ICSE 2022 Distinguished
Paper.
- **Gated, on request only**, quoted from the paper: *"we intentionally did not include
  direct links to them ... We will share our replication package with links only upon
  request for research purposes."* No license exists for the data.
- 100 GitHub issue threads, **all toxic by construction — no negative examples**.
  Categories (with counts): Insulting (55), Entitled (25), Arrogant (21), Trolling (17),
  Unprofessional (23), plus Target (code/people/other) and Trigger (including
  "technical disagreement," 22 threads) fields.
- Reported agreement — quoted: *"Cohen's unweighted kappa coefficient was 0.82 for 149
  comments with two independent labels and Fleiss' kappa coefficient was 0.72 for 43
  comments with three independent labels"* — this covers only the binary toxic/not-toxic
  screening step; the category labels themselves have **no reported agreement** (35
  issues coded by 2–3 people, the remaining 65 by one researcher).
- Mapping: `personal_attack` strong (Insulting + people-target), `hostility` strong
  (Insulting + Trolling), `status_authority_invocation` partial ("Arrogant" = "imposes
  view from a position of perceived authority"), `dismissiveness` partial (Arrogant is
  adjacent but framed as demand-backed-by-superiority, not bare non-engagement),
  `technical_disagreement` partial (exists only as a toxicity *trigger* tag, every
  example is already toxic). All 5 remaining argument labels, `sarcasm`, `gatekeeping`,
  `resolution_marker` — none. `tone_intensity` partial (binary severe-language flag
  only).
- Caveat: n=100, no negatives, gated with no license — usable at most as a **qualitative
  taxonomy reference** (the Entitled/Arrogant boundary is a genuinely useful contrast
  for calibrating `status_authority_invocation` and `dismissiveness`), not as licensed
  benchmark rows.

**20–22. Ferreira, Cheng, Adams & Rafiq — the incivility trilogy.**
- 20: *"The 'Shut the f\*\*k up' Phenomenon": Characterizing Incivility in Open Source
  Code Review Discussions*, PACMHCI 5(CSCW2), 2021, arXiv:2108.09905. 1,545 LKML
  patch-rejection emails, 262 threads, Jan 2018–Mar 2019. figshare `license` field:
  `"CC BY 4.0"` (quoted from the API response).
- 21: *How heated is it? Understanding GitHub locked issues*, MSR 2022,
  arXiv:2204.00155. 205 issues locked "too heated," 79 projects, 5,511 comments (718
  with a tone feature). figshare license: `"CC BY 4.0"`.
- 22: *Incivility Detection in Open Source Code Review and Issue Discussions*, Journal of
  Systems and Software 2024, arXiv:2206.13429 — reuses 20+21, no new annotation.
  **License conflict, both verified directly**: GitHub repo `iferreiradev/incivility_detection`
  reports `"spdx_id": "GPL-3.0"`; the figshare copy of the same data reports
  `"CC BY 4.0"`. **Use the figshare copy** and note the conflict if citing this dataset.
- Label scheme (20 and 21 share the same "Tone Bearing Discussion Feature," or TBDF,
  scheme, 16 categories in 20 expanded to 20 in 21): uncivil = bitter frustration,
  impatience, irony, mocking, name calling, threat, vulgarity (+ entitlement,
  insulting, identity attacks/name-calling as separately itemized in 21); civil =
  appreciation/excitement, considerateness, humility, friendly joke, hope for feedback,
  sincere apologies, commanding, oppression, sadness. Coded per sentence, full thread
  context available to coders.
- Agreement — quoted: 20's is *"the Kappa scores on all codes in the final version of
  the codebook ranged from 0.42 to 0.96, with an average of 0.62"*; 21's is *"the
  average Cohen's Kappa score between the two raters ... was 0.65, ranging from 0.43 to
  0.91"* (TBDF) and *"0.85, ranging from 0.64 to 1.00"* (justification codes). Both are
  2-author annotation with partial double-coding (20: only the 191 emails containing a
  TBDF were double-coded; 21: 20% double-coded).
- Mapping (both 20 and 21): `personal_attack` strong (name calling/identity attacks),
  `hostility` strong (bitter frustration, vulgarity, threat), `sarcasm` strong (irony,
  mocking — this is the **best sarcasm match found in the SE-domain search**),
  `dismissiveness` partial (impatience/commanding are adjacent but not identical),
  `acknowledgment` partial (humility, appreciation/excitement), `technical_disagreement`
  partial (only a technical/non-technical topic tag, not a stance label). All other
  labels — none.
- Notable methods finding from 22: adding the previous email/comment as model context
  **made BERT predictions worse** and classifiers did not transfer between LKML and
  GitHub domains — directly relevant to this project's own parent-context design
  decision, worth a cross-reference in `COMMUNITY-HEALTH.md` §4 if not already there.
- Caveat: only 2 raters (not this project's ≥3), per-code kappa as low as 0.42–0.43,
  uncivil text is only ~7–9% of the data (a few hundred sentences), selection bias
  toward rejected patches / locked issues. A related independent study (Bock et al., JSS
  2025, DOI 10.1016/j.jss.2025.112339) reportedly found low human agreement on LKML
  aggressiveness — flagged by a sub-agent via Semantic Scholar but not independently
  re-verified here.

**23. Ehsani, Imran, Zita, Damevski & Chatterjee (2024), GitHub incivility** —
`vcu-swim-lab/incivility-dataset`, MIT (quoted LICENSE).
- 404 locked GitHub issue threads / 5,961 comments, 213 projects (≥50 contributors),
  2013–2023. 9 comment-level uncivil TBDF categories (bitter frustration, impatience,
  mocking, irony, vulgarity, threat, entitlement, insulting, identity attacks/name-
  calling — 23% of comments got a label) plus thread-level Trigger (including
  "Technical disagreement"), Target, and Consequence (including "Turning constructive,"
  "Accepting criticism").
- **Material quality gap**: 19 CS-student annotators, each covering their own ~20
  threads independently — **no item was rated by more than one human**. No Cohen's
  kappa/Krippendorff's alpha exists; quality control instead used GPT-4 to flag
  low-confidence annotations (549/5,961 flagged) for author re-review. This is a
  meaningfully weaker provenance than this project's ≥3-human-rater design and should
  not be treated as having a usable IAA.
- Mapping: `personal_attack` strong, `hostility` strong, `sarcasm` strong,
  `dismissiveness` partial, `resolution_marker` partial (via "Turning constructive" /
  "Accepting criticism" consequence tags, thread-level not message-level).
  `gatekeeping`/`status_authority_invocation` — none (no tenure/authority-based
  dismissal category exists in the schema). All 6 argument labels — none (only uncivil
  comments get a substantive label; everything else is "None").
- Caveat: largest, most permissively licensed, most recent (2024) SE incivility set
  found — but the missing human IAA is disqualifying for treating it as gold-standard
  ground truth without independent re-annotation of at least a sample.

**24. Ortu et al., JIRA emotions** — mirrored at `collab-uniba/EMTK_datasets/jira/emotions`,
MIT (quoted LICENSE).
- ~4,000 JIRA issues, Ekman-style emotion labels collapsed to love/joy (→positive),
  anger/sadness/fear (→negative, fear discarded per README), neutral. Annotator type
  and IAA **UNVERIFIED** — not stated in the mirror's README, and the original PROMISE
  2015/MSR 2016 paper access points could not be independently reached this pass.
- Mapping: `hostility` partial (anger label conflates hostility, personal attack, and
  plain frustration without distinguishing target), `acknowledgment` partial
  (love/joy as a weak positive-recognition proxy). All discourse-act labels and
  `tone_intensity` — none (Ekman emotion ≠ our constructs).

**25. Novielli, Calefato, Lanubile et al. — EmoTxt (SO emotion gold standard) / Senti4SD
(SO polarity gold standard)** — `collab-uniba/EmotionDatasetMSR18`, `collab-uniba/
Senti4SD`, `collab-uniba/EMTK_datasets`.
- Senti4SD/EMTk repos: MIT (quoted). The MSR18 emotion repo itself has **no LICENSE
  file** (GitHub API confirms `license: null`); its only usage term is a citation
  request in the README ("Fair Use Policy"), which is not a formal redistribution
  grant.
- 4,800 Stack Overflow posts (emotion) + 4,424 (polarity, 3,098 train / 1,326 test).
  Annotator type and IAA for both **UNVERIFIED** (not stated in the fetched READMEs;
  would require reading the underlying EMSE/MSR papers in full).
- Mapping: `hostility` partial (anger), `acknowledgment` partial (love/joy/positive
  polarity). All discourse-act labels and `tone_intensity` — none.
- Note: domain is Stack Overflow **generally**, not code-review or mailing-list
  discussion specifically, and skews toward Q&A register rather than disagreement/
  argument register.

**Ruled out entirely (searched, not found, or not a usable message corpus):**
- **"Destructive criticism in code review"** (Gunawardena et al. 2022, PACMHCI/CSCW2,
  DOI 10.1145/3555183) — verified via Crossref/Semantic Scholar abstract: this is a
  **93-respondent survey rating hypothetical vignettes**, not a corpus of real labeled
  messages. Not usable.
- **"Pushback in code review"** (Egelman et al. 2020 ICSE; Murphy-Hill et al. 2022
  CACM) — both analyze **Google-internal** Critique review logs and employee survey
  data. No public dataset exists; the related Kononenko/Bird line of work on review
  quality likewise released no public dataset. **Not found.**
- **Stack Overflow comment civility/toxicity** (as a construct distinct from
  emotion/polarity) — no dedicated dataset found; closest resources are #25 above.
- **Apache Software Foundation mailing-list civility/sentiment/toxicity dataset** —
  extensively searched; no such dataset exists publicly. Prior Apache-mailing-list
  studies found (Rigby & Hassan 2007; Colaco Junior et al. 2010) are linguistic/
  statistical analyses of raw archives, not released annotated corpora. **Not found.**

### 2.2 General online-discussion datasets

**1. Wikipedia Personal Attacks / Aggression corpora (Wulczyn, Thain & Dixon, 2017)** —
*Ex Machina: Personal Attacks Seen at Scale*, WWW 2017, arXiv:1610.08914.
- License confirmed via figshare API: `"license": {"value": 2, "name": "CC0", ...}` for
  the data; the paper itself is "©2017 IW3C2, published under CC BY 4.0."
- 115,737 comments total (random 37,611 + "later-blocked-user" oversample 78,126), each
  rated by 10 crowd workers via Crowdflower. Krippendorff's α = 0.45, quoted directly
  from the paper — **below this project's own 0.667 "usable" gate (§6.3)**.
- Mapping: `personal_attack` strong (this is the literal annotated construct);
  `hostility`/`tone_intensity` partial via the separate Aggression corpus's −3..+3
  aggression score (bipolar, not our unidirectional 0–4 scale). All 10 remaining
  labels — none.
- Value: enormous volume (115k) is useful for **precision-floor calibration** of
  `personal_attack` (statistical power to hit the ≥0.85 precision target) even though
  its own IAA would not itself clear this project's alpha gate — i.e., treat it as
  volume for testing Jev's precision, not as a source of "gate-clearing" ground truth.

**2–3. ConvoKit "Conversations Gone Awry" — CGA-WIKI and CGA-CMV/CGA-CMV-Large.**
- CGA-WIKI (Zhang et al., ACL 2018): 4,188 conversations / 30,021 comments, Wikipedia
  article-talk pages, `comment_has_personal_attack` binary label from 3 crowd
  annotators + internal verification. **No license found on the ConvoKit
  documentation page** — UNVERIFIED, unlike the Politeness Corpus page which does state
  one.
- CGA-CMV / CGA-CMV-Large (Chang & Danescu-Niculescu-Mizil, EMNLP 2019; extended 2024):
  6,842–19,578 conversations, Reddit r/ChangeMyView. Same binary-attack framing; license
  and per-item annotator/IAA UNVERIFIED.
- Mapping (both): `personal_attack` strong, `hostility` partial, `tone_intensity` none
  (binary label only), all other labels none.
- Structural note: both release **full conversational sequence up to the point of
  interest**, so parent/preceding-message context is inherent to the format — the
  closest structural match to this project's "message + immediate parent" design among
  the general-domain candidates, even though the label itself is narrow.

**4–5. WikiDisputes and WikiTactics (De Kock, Stafford & Vlachos, 2021/2022).**
- WikiDisputes (EACL 2021, arXiv:2101.10917): 7,425 Wikipedia content-dispute
  conversations, GitHub `christinedekock11/wikidisputes` reports `license: null`
  (UNVERIFIED). Conversation-level `escalation_label` is an **administrative** fact
  (referred to Wikipedia mediation or not), not a human-rated construct — no IAA
  applies. Mapping: `resolution_marker` partial (coarse inverse proxy, conversation-
  level only), `technical_disagreement` partial (all items are disputes by
  construction, no per-message label), everything else none.
- WikiTactics (EMNLP 2022, arXiv:2212.08353): 213 disputes / 3,865 utterances, GitHub
  `christinedekock11/wikitactics` also reports `license: null` (UNVERIFIED; the paper
  page itself displays what may be a CC BY-NC-SA marker, but that is not confirmed to
  cover the released JSON). **This is the taxonomically closest published match to our
  scheme found anywhere in this search**: an ordered rebuttal hierarchy RH0 (direct
  insults/hostile tone) → RH1 (attacks on credibility) → RH2 (off-topic) → RH3
  (policing/tone-correction) → RH4 (restating stance) → RH5 (counterargument with new
  reasoning) → RH6 (refutation addressing why an argument is mistaken) → RH7 (refuting
  the central point), plus unordered "coordination" tactics (questions, clarification,
  **suggesting compromise**, conceding/recanting, bailing out). Mapping: `personal_attack`
  strong (RH0/RH1), `technical_disagreement` strong (RH4–RH7), `constructive_
  counterargument` strong (RH5/RH6's definitions are close paraphrases of ours),
  `evidence_based_argument` partial (RH5 covers "new reasoning/evidence" without
  separately flagging citations/benchmarks), `compromise_offer` strong ("suggesting
  compromise"), `acknowledgment` partial ("conceding/recanting," "clarification"),
  `hostility` partial, `dismissiveness` partial (RH2/RH3), `gatekeeping` partial (RH3
  "policing, citing policy" conflates legitimate routing with standing-denial),
  `resolution_marker` partial (inherited WikiDisputes label). `sarcasm` and
  `status_authority_invocation` — none.
- **Critical caveat**: WikiTactics' main annotation pass was **a single annotator**;
  the reported Cohen's κ (0.17→0.55 across three pilot rounds, Pearson 0.68 in the
  final round) covers only a 2-annotator pilot, never the released 3,865-utterance
  set. There is **no multi-rater agreement on the actual labels being distributed** —
  a serious gap against this project's ≥3-rater design. Best treated as a **schema/
  methodology reference** (the RH0–RH7 hierarchy is a genuinely useful design pattern
  to borrow) rather than as importable gold-standard rows.

**6. Stanford Politeness Corpus** (Danescu-Niculescu-Mizil et al., ACL 2013) — Wikipedia
+ Stack Exchange requests, via ConvoKit.
- License quoted from ConvoKit page: *"ConvoKit's Stanford Politeness Corpus is
  governed by the CC BY license v4.0."*
- 4,353 (Wiki) + 6,604 (SE) annotated requests out of much larger available pools; 5
  MTurk annotators per request, vetted (US residency, linguistic questionnaire,
  attention-check). IAA reported as mean pairwise Pearson correlation (continuous
  labels, not categorical): ≈0.68 (Wiki) / ≈0.58 (SE); full-5-annotator agreement on
  the binarized label ranges 3–62% depending on politeness-score quartile (quoted from
  Table 2).
- Mapping: `hostility`/`tone_intensity` partial (politeness is bipolar
  friendly↔unfriendly, not our unidirectional 0–4 scale, and rates *requests*
  specifically). All 11 other labels — none.
- Caveat: annotators deliberately saw only a fixed 2-sentence excerpt (context
  intentionally minimized, the opposite of a "message + parent" design); Stack Exchange
  portion spans many non-programming sites (gardening, cycling), not
  programming-specific.

**7–8. Jigsaw Toxic Comment Classification Challenge (2017/18) and Civil Comments /
Unintended Bias (2019).**
- Toxic Comment: derivative of the Wulczyn Wikipedia Detox corpus, ~223,000 comments
  (159,571 train), labels `toxic`, `severe_toxic`, `obscene`, `threat`, `insult`,
  `identity_hate` (binary, multi-label). HF mirror states CC0; Kaggle's own page could
  not be rendered (JS-only) so its license text is only partially verified via the HF
  mirror.
- Civil Comments: Borkan et al., *Nuanced Metrics for Measuring Unintended Bias*, WWW
  2019 companion, arXiv:1903.04561 — quoted from the paper: "a new human-labeled dataset
  of nearly 2 million comments ... 450,000 comments annotated with the identities that
  are referenced." Continuous [0,1] scores for `toxicity`, `severe_toxicity`, `obscene`,
  `threat`, `insult`, `identity_attack`, `sexual_explicit`. HF card states CC0.
- Mapping (both): `personal_attack` strong (`identity_hate`/`identity_attack`,
  `insult`), `hostility` partial, `tone_intensity` partial (Civil Comments' continuous
  `toxicity` score is the **best available general-domain proxy for a graded intensity
  axis** among all candidates, though it measures "rudeness" broadly, not our specific
  rubric). All 10 other labels — none.
- Caveat: news-comment-section register (Civil Comments) is politically charged and
  anonymous in a way SE technical discussion is not; both sets exist explicitly to
  study demographic-bias artifacts in toxicity labels, which is a known confound if
  reused naively.

**9. SARC (Self-Annotated Reddit Corpus)** — Khodak, Saunshi & Vodrahalli, LREC 2018.
- Code repo `NLPrinceton/SARC` is MIT; the data itself (hosted separately on a Princeton
  server) carries **no explicit license text** — UNVERIFIED for the data specifically.
- Labels come entirely from the Reddit `/s` self-tag — **distant/weak supervision, not
  human annotation**; no IAA applies structurally. iSarcasm's own authors (see below)
  found `/s`-derived positives disagree substantially with human-perceived sarcasm.
- Mapping: `sarcasm` partial at best (noisy proxy, not gold-standard human judgment).
  All other labels — none.

**10. iSarcasm / iSarcasmEval** — Oprea & Magdy, ACL 2020; Abu Farha et al., SemEval
2022 (extension).
- iSarcasm's own GitHub link (cited in the paper) is dead (404, confirmed); the dataset
  remains reachable via the ACL Anthology's own attachment host. License: no formal
  grant, only "we publish the dataset publicly for research purposes" — treat as
  research-use-only, redistribution UNVERIFIED. iSarcasmEval is separately confirmed
  MIT-licensed and larger.
- 4,484 tweets (~747 sarcastic), labeled by **the original tweet's own author**
  (self-labeled sarcastic + non-sarcastic tweets with rephrasings), plus a separate
  crowd-annotation pass for comparison. Crowd annotators matched the author's own label
  with only **F=0.616** — the paper's headline finding, and a meaningful caveat for
  any *third-party*-rated sarcasm dataset (including SARC and the Internet Argument
  Corpus below): third-party-perceived sarcasm and author-intended sarcasm are
  measurably different constructs.
- Mapping: `sarcasm` strong (best-validated ground-truth methodology for sarcasm found
  in this search, with explicit subtype labels — irony, satire, understatement,
  overstatement, rhetorical question). All other labels — none.

**11. IBM Debater Evidence Sentences family.**
- The canonical `research.ibm.com/haifa/dept/vst/debating_data.shtml` page now 404s
  live; content recovered via a Wayback Machine snapshot. Quoted license from that
  snapshot: *"© Copyright Wikipedia. © Copyright IBM 2014. Released under CC-BY-SA."*
  Quoted access gate: *"To download, please fill in the request forms below."*
- Multiple sub-datasets (5,785 topic–sentence pairs binary "is this valid evidence,"
  118 topics; 29,429 pairs with a continuous 0–1 evidence-strength score from Figure
  Eight crowd workers, 222 topics; 30,497-argument and 6.3k/14k-pair argument-quality
  sets). No IAA statistic found on the fetched page for any sub-set.
- Mapping: `evidence_based_argument` strong for the Evidence Sentences sub-sets (close
  to our "checkable artifact" criterion, though sourced from Wikipedia prose, not
  technical discussion). All interpersonal/dialogic labels — none (single-sentence,
  no-parent-context, no dialogue-turn structure at all).
- Caveat: request-form gated, canonical hosting page currently dead — actual per-
  dataset download mechanics were not tested end-to-end this pass.

**12. UKP Sentential Argument Mining Corpus** (Stab et al., EMNLP 2018).
- **Both of the paper's own cited access points failed during verification**: the TU
  Darmstadt TUdatalib handle is behind a bot-protection wall ("Anubis") that blocked
  every attempt, and the paper's own short-link (`ukp.tu-darmstadt.de/sent_am`) fails
  with a TLS certificate mismatch. License and confirmed size/download mechanics are
  therefore **UNVERIFIED** beyond what the paper PDF itself states (27,520 sentences,
  8 topics).
- 3-way label (supporting argument / opposing argument / non-argument) on isolated
  sentences vs. a topic — not message-vs-message. 2 expert annotators, Cohen's κ =
  0.721 on a 200-sentence evaluation subset (quoted, exceeds the paper's own 0.7
  reliability threshold); full corpus scaled via MTurk + MACE denoising.
- Mapping: `evidence_based_argument` partial (support/oppose ≠ our stricter
  "checkable" bar). All dialogic labels — none (single-sentence-vs-topic, no
  parent/reply pairing at all, by explicit authorial design).
- Caveat: **highest access risk of any candidate reviewed** — re-verify with a human
  browser session (which can pass the bot-wall) before relying on this source at all.

**13. TalkDown** (Wang & Potts, EMNLP-IJCNLP 2019) — condescension detection.
- GitHub `zijwang/talkdown` repo license: AGPL-3.0 (repo-level; no separate data-only
  license file was found).
- 4,992 valid instances (65.2% condescending / 34.8% not, after EM label aggregation);
  a separately built balanced (3,255/3,255) and imbalanced 1:20 (3,255/65,100) version
  also exist. Reddit comment/reply pairs where the reply directly quotes a span from
  the comment. Fleiss' κ = 0.593 (quoted, "moderate to substantial" per Landis & Koch),
  MTurk crowd workers with a qualifying pre-task, labels EM-aggregated rather than
  majority-vote.
- **Structural note: this is the closest match to our exact unit of analysis found
  anywhere in this search.** Each example ships the quoted span, the full COMMENT it
  came from, the accusing REPLY, and the preceding CONTEXT — i.e., message + immediate
  parent, exactly this project's design.
- Mapping: `gatekeeping` strong (condescension-as-flagged-by-a-reply is closely
  adjacent when it implies the target lacks standing/competence, though TalkDown's
  definition is broader — also covers insincere praise), `status_authority_invocation`
  partial (condescension often implies an unstated "I know better" stance, but the
  label is on the *reply's accusation*, not on whether the original speaker explicitly
  invoked their own authority), `personal_attack` partial, `dismissiveness`/`sarcasm`
  partial (frequently co-occur but aren't synonymous), `hostility` partial. All 6
  argument labels and `resolution_marker` — none. `tone_intensity` none directly
  (binary label), though the QUOTED/CONTEXT structure would support re-annotation for
  intensity.
- Caveat: condescension is inherently low-base-rate and, per the authors' own framing,
  "impossible to detect from isolated utterances" — exactly the parent-context problem
  this project is already designed around, which is why this dataset is a good
  structural fit despite the label mismatch. AGPL-3.0's copyleft terms should be
  reviewed before redistributing anything beyond aggregate results.

**14. DEBAGREEMENT** (Pougué-Biyong et al., NeurIPS 2021 Datasets & Benchmarks Track).
- License quoted directly from OpenReview submission metadata: *"Creative Commons
  Attribution 4.0 International Public License ('CC BY 4.0')."*
- 42,894 comment-reply pairs, 5 subreddits (r/BlackLivesMatter, r/Brexit, r/climate,
  r/democrats, r/Republican), Pushshift-sourced. Labels: agree / disagree / neutral /
  unsure, each pair annotated by **"at least three raters"** (quoted from the archived
  distribution page) — this is the **only general-domain candidate confirmed to use
  ≥3 raters per item**, matching this project's own design. Distributed in three
  confidence tiers: Full Agreement, 2-of-3+ Agreement, and a "Challenge" (no-consensus)
  split — a genuinely useful methodological pattern.
- **Access risk**: the paper's own canonical `dataset_url` (a Scale AI "Open Datasets"
  page) has been repurposed for an unrelated product and no longer serves this data;
  content was recovered via a 2022 Wayback snapshot. Only unverified third-party GitHub
  mirrors were found as alternates.
- Mapping: `technical_disagreement` partial (agree/disagree axis is topic-general, not
  technical-claim-specific, but the dialogic shape is right), `constructive_
  counterargument` partial (disagree-labeled pairs aren't guaranteed to engage the
  specific claim with a reason — our stricter bar), `resolution_marker` partial
  ("agree" is adjacent to but not synonymous with explicit closing language). All other
  9 labels and `tone_intensity` — none.
- Caveat: politically polarized subreddits are near-maximal distance from SE technical
  discourse in both tone and topic; the authors themselves note the data "contains
  slang, sarcasm and topic-specific jokes."

**15. Internet Argument Corpus (IAC / IAC v2)** (Abbott et al., LREC 2016; builds on
Walker et al., LREC 2012).
- License: **UNVERIFIED** — no formal license text found on either the live successor
  page or in the LREC 2016 paper; only an informal "available ... for free research
  use" characterization.
- 414,453 posts / 11,079 threads (4forums), 65,368 posts (ConvinceMe), plus a smaller
  CreateDebate gun-control subset (all counts quoted from the paper's Table 1). A
  9,975-pair quote-response subset of 4forums carries the richest labels: continuous
  [-5,+5] mean-annotator scores for Disagree/Agree, Attacking/Respectful,
  Emotion/Fact, and Nasty/Nice, plus a %-Yes sarcasm question — all from Mechanical
  Turk workers. No single reliability statistic (kappa/alpha) was found in the 2016
  paper for these axes; it may exist in the earlier 2012 IAC 1.0 paper (not
  independently re-checked this pass).
- **Structural note**: annotations are explicitly over quote/response pairs, i.e.
  message + immediate parent is already the unit of analysis — matches this project's
  format, and it is the **only candidate offering disagreement, hostility, fact-vs-
  emotion, and sarcasm labels together on the same items**.
- Mapping: `technical_disagreement`/`constructive_counterargument` partial-to-strong
  (graded Disagree/Agree axis over quote-response pairs), `evidence_based_argument`
  partial (Emotion/Fact axis is a coarse proxy), `hostility` strong, `personal_attack`
  partial (Attacking/Respectful doesn't distinguish attack-on-person from attack-on-
  argument, our key boundary), `sarcasm` strong, `tone_intensity` partial-to-strong
  (the continuous multi-axis scores are the best available general-domain proxy for a
  graded intensity signal). `compromise_offer`, `acknowledgment`, `resolution_marker`,
  `dismissiveness`, `gatekeeping`, `status_authority_invocation` — none.
- Caveat: political/social debate-forum register (evolution, gay marriage, abortion,
  existence of God — the paper's own example topics) is far from SE technical
  discussion topically; both of the corpus's homepage URLs have moved at least once in
  its history and the current access mechanism (a Google Drive link plus an email
  contact) is informal, not a stable versioned host — treat long-term reproducibility
  as a real risk requiring direct maintainer contact before committing to this source.

---

## 3. Coverage matrix — our 12 labels + tone_intensity × datasets

`S` = strong, `P` = partial, `—` = none. Rows ordered as in §2. Columns: TD =
technical_disagreement, CC = constructive_counterargument, EB = evidence_based_argument,
CO = compromise_offer, AK = acknowledgment, PA = personal_attack, HO = hostility,
DI = dismissiveness, SA = sarcasm, GK = gatekeeping, SAI = status_authority_invocation,
RM = resolution_marker, TI = tone_intensity.

| Dataset | TD | CC | EB | CO | AK | PA | HO | DI | SA | GK | SAI | RM | TI |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Wikipedia Personal Attacks/Aggression | — | — | — | — | — | **S** | P | — | — | — | — | — | P |
| ConvoKit CGA-WIKI | — | — | — | — | — | **S** | P | — | — | — | — | — | — |
| ConvoKit CGA-CMV(-Large) | — | — | — | — | — | **S** | P | — | — | — | — | — | — |
| WikiDisputes | P | — | — | — | — | — | — | — | — | — | — | P | — |
| WikiTactics | **S** | **S** | P | **S** | P | **S** | P | P | — | P | — | P | — |
| Stanford Politeness Corpus | — | — | — | — | — | — | P | — | — | — | — | — | P |
| Jigsaw Toxic Comment Classification | — | — | — | — | — | **S** | P | — | — | — | — | — | — |
| Jigsaw Civil Comments | — | — | — | — | — | **S** | P | — | — | — | — | — | P |
| SARC | — | — | — | — | — | — | — | — | P | — | — | — | — |
| iSarcasm / iSarcasmEval | — | — | — | — | — | — | — | — | **S** | — | — | — | — |
| IBM Debater Evidence Sentences | — | — | **S** | — | — | — | — | — | — | — | — | — | — |
| UKP Sentential Argument Mining | — | — | P | — | — | — | — | — | — | — | — | — | — |
| TalkDown | — | — | — | — | — | P | P | P | P | **S** | P | — | — |
| DEBAGREEMENT | P | P | — | — | — | — | — | — | — | — | — | P | — |
| Internet Argument Corpus (IAC v2) | P/S | P/S | P | — | — | P | **S** | — | **S** | — | — | — | P/S |
| Sarker/Turzo/Bosu 2020 + ToxiCR | — | — | — | — | — | P | P | — | — | — | — | — | — |
| Raman et al. 2020 | — | — | — | — | — | P | P | — | — | — | — | — | P |
| Miller et al. 2022 | P | — | — | — | — | **S** | **S** | P | — | — | P | — | P |
| Ferreira LKML ("Shut the f\*\*k up") | P | — | — | — | P | **S** | **S** | P | **S** | — | — | — | — |
| Ferreira GitHub locked issues | P | — | — | — | P | **S** | **S** | P | **S** | — | — | — | — |
| Ferreira/Rafiq combined classifier data | P | — | — | — | P | **S** | **S** | P | **S** | — | — | — | — |
| Ehsani et al. 2024 GitHub incivility | — | — | — | — | — | **S** | **S** | P | **S** | — | — | P | — |
| Ortu JIRA emotions | — | — | — | — | P | — | P | — | — | — | — | — | — |
| Novielli/Calefato/Lanubile EmoTxt/Senti4SD | — | — | — | — | P | — | P | — | — | — | — | — | — |

**Reading the matrix**: `technical_disagreement`, `constructive_counterargument`,
`evidence_based_argument`, and `compromise_offer` each have at most one strong external
source, and it is the same low-provenance one (WikiTactics, single-annotator).
`acknowledgment`, `gatekeeping` (beyond TalkDown's partial-condescension proxy),
`status_authority_invocation`, and `resolution_marker` have **no strong external
source at all**. `personal_attack`, `hostility`, and `sarcasm` are comparatively
well covered, especially by the SE-domain Ferreira/Ehsani family. `tone_intensity`
as this project defines it (a 0–4 unidirectional neutral→aggressive scale) has no
direct external analog anywhere in this search; the closest proxies are all bipolar
or coarse (IAC v2's continuous Nasty/Nice, Jigsaw Civil Comments' continuous
toxicity score).

---

## 4. Recommended shortlist

Ranked by (a) actual license clarity, (b) taxonomy fit, (c) domain proximity to
Cassandra's dev@/user@/JIRA/GitHub-PR sources, (d) annotation provenance.

| Priority | Dataset | Why | Est. items to use |
|---|---|---|---|
| 1 | Ferreira, Cheng & Adams — LKML incivility ("Shut the f\*\*k up", 2021) | Only clean-licensed (CC BY 4.0) dataset whose domain — a mailing list, rejected-patch threads — most closely resembles Cassandra's dev@ list; strong on personal_attack/hostility/sarcasm | 1,545 (use all) |
| 2 | Ferreira, Adams & Cheng — GitHub locked issues ("How heated is it?", 2022) | Same label scheme and license (CC BY 4.0) as #1, but on the GitHub side of our sources; combining both gives cross-venue coverage of the same three strong labels | ~2,000 (all 718 TBDF-tagged comments + a matched sample of non-TBDF negatives) |
| 3 | TalkDown (Wang & Potts, 2019) | Only dataset with a message+immediate-parent structure matching our design exactly, and the only usable signal for `gatekeeping`/`status_authority_invocation` found anywhere; Fleiss κ = 0.593 is a real, reported multi-rater figure | 4,992 (use all) |
| 4 | DEBAGREEMENT (Pougué-Biyong et al., 2021) | Only general-domain candidate confirmed to use ≥3 raters/item (matches our design) and clean CC BY 4.0 license; best available volume for `technical_disagreement`/`constructive_counterargument` pressure-testing despite off-topic domain | 3,000 (stratified sample across the Full/2-of-3/Challenge confidence tiers) |
| 5 | Wikipedia Personal Attacks / Aggression (Wulczyn et al., 2017) | Cleanest license (CC0) and largest volume (115,737) of any candidate; use for **precision-floor calibration** of `personal_attack` (statistical power to test the ≥0.85 precision gate) even though its own α=0.45 would not itself clear our gate as ground truth | 5,000 (random sample; treat Jev-vs-this-corpus precision as informative, not gating) |
| 6 (optional) | Ehsani et al. 2024 GitHub incivility | Largest, most recent, most permissively licensed (MIT) real-GitHub incivility set; adds `resolution_marker`-adjacent signal via "Turning constructive" tags, but its single-annotator provenance (no human IAA) means it should be a volume supplement, not a primary source | 3,000 (the 1,365 TBDF-tagged comments + a matched negative sample) |

**Combined shortlist volume**: ~1,545 + 2,000 + 4,992 + 3,000 + 5,000 (+3,000 optional)
≈ 16,537 items (≈19,537 with #6).

**What this shortlist does not cover — gaps requiring Cassandra-domain calibration:**
- `evidence_based_argument`, `compromise_offer`, `acknowledgment`, `resolution_marker`:
  no public dataset in this search offers a strong, well-provenanced source for any of
  these. WikiTactics is the only strong conceptual match and its single-annotator
  provenance disqualifies it as gold-standard ground truth without re-annotation.
- `gatekeeping` and `status_authority_invocation`: these are specifically about
  OSS/ASF governance structure — committer/PMC standing, "who gets to decide" — a
  construct essentially absent from Wikipedia-editor, Reddit, or generic web-comment
  data, and only partially approximated by TalkDown's condescension label. This is
  very likely genuinely novel territory requiring Cassandra-specific (or at least
  ASF-wide) expert-labeled examples; no public dataset found here should be expected to
  clear the friction-group precision/recall gate (§6.4) for these two labels on its
  own.
- `tone_intensity` as a 0–4 unidirectional, "describe the situation not the degree"
  scale: no dataset in this search encodes this shape directly. IAC v2's continuous
  multi-axis scores and Jigsaw Civil Comments' continuous toxicity score are the
  closest available proxies but would need remapping and validation against Cassandra
  examples before use.
- Even where public datasets score "strong" (personal_attack, hostility, sarcasm),
  every one of them is Wikipedia-editor, Reddit, or generic-GitHub/LKML register —
  none is Cassandra-specific, and Miller et al. (2022) explicitly found that OSS
  toxicity looks qualitatively different from general-web toxicity (entitlement and
  dismissiveness of maintainer effort, not slurs). A small Cassandra-domain
  calibration slice remains necessary even for the best-covered labels, to confirm the
  external-corpus precision/recall figures actually hold on this project's own venues
  (dev@/user@ mailing lists, JIRA comments, GitHub PR review comments).

---

## 5. Jev cost estimate

Per COMMUNITY-HEALTH.md, one classification call per message produces all 12 label
answers plus `tone_intensity` together (parallel Nouls/Score over the same state), so
cost is per-item, not per-label. At the stated rate of **~2,900 input tokens/item** and
**$0.042 per 1,000,000 input tokens**:

- Cost per item = 2,900 × ($0.042 / 1,000,000) ≈ **$0.0001218/item** (about 0.012 cents).

| Scope | Items | Estimated Jev input-token cost |
|---|---|---|
| Shortlist item 1 (Ferreira LKML) | 1,545 | ≈ $0.19 |
| Shortlist item 2 (Ferreira GitHub locked issues) | 2,000 | ≈ $0.24 |
| Shortlist item 3 (TalkDown) | 4,992 | ≈ $0.61 |
| Shortlist item 4 (DEBAGREEMENT) | 3,000 | ≈ $0.37 |
| Shortlist item 5 (Wikipedia Personal Attacks/Aggression) | 5,000 | ≈ $0.61 |
| Shortlist item 6, optional (Ehsani GitHub incivility) | 3,000 | ≈ $0.37 |
| **Core shortlist (items 1–5)** | **16,537** | **≈ $2.01** |
| **Full shortlist (items 1–6)** | **19,537** | **≈ $2.38** |
| For comparison: this project's own frozen benchmark target (§6.1) | 3,000–5,000 | ≈ $0.37–$0.61 |

At this rate, the Jev inference cost of running the classifier against the entire
recommended external shortlist is trivial (a few dollars) relative to the cost of
building the human-labeled ground truth itself (≥3 raters × thousands of items). The
binding constraint on using these datasets is **license clarity and annotation
provenance** (several strong taxonomic matches — WikiTactics, Ehsani et al., several
ConvoKit corpora — have UNVERIFIED licenses or no usable multi-rater human IAA), not
compute cost.
