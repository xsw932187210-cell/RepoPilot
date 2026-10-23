# CH-01C predeclared comparison — 2026-10-23

This plan precedes all new repair inference. CH-01B routing code is implemented;
its consolidated Docker acceptance is still required before this plan is run.
The experiment is a small descriptive pilot, with no assumed improvement.

The tracked protocol is `evals/real/experiments/ch01c-pilot-v1.json`. It selects the
three frozen holdout defects (tqdm 1, Cookiecutter 2), `oneshot / workflow / auto`,
two repeats, rotating order, at most six API attempts per defect/strategy/repeat,
and at most 108 total attempts including failures, unknown outcomes and retries.
The historical The Fuck 20 are not included. No additional connection calls or
rerunning failed terminal units is part of this budget.

Primary model: `google_cloud / gemini-3.5-flash`, temperature 1.0. Planner and Coder
use the same profile in all arms where that role exists. Workflow review uses
`chatgpt / gpt-6.1-sol`; one-shot omits model planning/review by definition. The
Coder/Reviewer prompts, full-file retrieval budgets, write policy, visible command
and independent hidden acceptance remain fixed. This compares whole execution
strategies; it cannot separately attribute differences to individual roles.

Gemini uses a predeclared 32,768 output-token ceiling because the shared complete-
file JSON edit contract can exceed 4,096 tokens for allowed source files. This is
a generic output-contract choice before inference, not a response to hidden
results. ChatGPT has no server output-token hard cap: existing local limits remain
90 seconds/65,536 streamed bytes. The observed SDK versions and exact routing
policy are bound into the protocol identity; no provider sampling seed is sent.
The final implementation digest must be recorded before `--run` is accepted.

Primary outcome is independently accepted repair with no pass-to-pass regression.
Approval gate reached, regressions, runtime failures, pending units, API attempts,
observed usage, time and routing decisions are reported separately. The same
visible-test/HITL graph gates all strategies; benchmark runs stop before human
decision/publication, then trusted hidden acceptance runs without feedback to the
model. A one-shot candidate that passes visible tests but fails hidden acceptance
does not trigger an auto upgrade. No benchmark PR is created.

There are three independent defects in two held-out projects, not 18 independent
defects. Difficulty is explicitly `unlabeled`: no independent difficulty rubric
exists, and answers must not supply invented labels. Reports include that stratum
and project/repeat/paired breakdowns. Project-cluster intervals are descriptive and
unstable at this scale; pending pairs cannot establish a winner. Missing usage and
money remain unknown. Public training contamination is unknown; curator QA saw
reference changes, but no holdout prompt/router tuning was performed.

The default command is read-only metadata planning:

```sh
python -m repopilot.eval_protocol
```

Actual inference is opt-in, using the already approved local grant:

```sh
python -m repopilot.eval_protocol --run \
  --source tqdm=reports/real-corpus-cache/tqdm \
  --source cookiecutter=reports/real-corpus-cache/cookiecutter
```

The private grant in `.local/evaluation-authorization/ch01c.json` is never created
by this command. It binds the exact public plan fingerprint and one output path,
`reports/ch01c-holdout-20261023-v1`. Shared reservations are mirrored into the
private grant before transport. A different directory/configuration cannot reset
it, missing/truncated reservation histories are rejected, and a failed observer
consumes the conservative cohort reservation without sending a request. The
grant and cohort locks exclude concurrent authorized runs. Resume uses the same
command and directory; quota retains pending units and completed results.

Results will be written after observation in a separate dated report. The
2026-09-09 results and failure ledger remain unchanged, including the historical
workflow result being below one-shot. Neither preparation nor Mock tests establish
new real-model repair quality.
