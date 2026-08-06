# Case study — three-part contribution to `huggingface/peft`

Three parameter-efficient fine-tuning methods surfaced from arXiv, drafted on the [`smellslikeml/peft`](https://github.com/smellslikeml/peft) fork, and shepherded upstream to `huggingface/peft` — where the tuner surface is used directly by five of six major fine-tuning frameworks per Scaling DoRA's Appendix G.

---

## 1. Riemannian Preconditioned LoRA — merged 2026-08-03

**Paper**: Zhang & Pilanci, *Riemannian Preconditioned LoRA for Fine-Tuning Foundation Models* — [arXiv:2402.02347](https://arxiv.org/abs/2402.02347)
**Upstream PR**: [`huggingface/peft#3382`](https://github.com/huggingface/peft/pull/3382) — merged (+401 / −3, 6 files, 32.7-day review latency)
**Fork branch**: [`feat/riemannian-preconditioned-lora`](https://github.com/smellslikeml/peft/tree/feat/riemannian-preconditioned-lora)
**Coordination**: [`huggingface/peft#3380`](https://github.com/huggingface/peft/issues/3380)

Replaces the Euclidean gradient with the natural gradient under the Riemannian metric induced by the low-rank matrix manifold. Aligns optimizer steps with the intrinsic tangent space and quotients the (A, B) ↦ (A·C, C⁻¹·B) reparameterization gauge that Euclidean SGD/Adam depend on.

![Riemannian Preconditioned LoRA — geometric intuition](figures/riemannian_lora.png)

---

## 2. Super-Tuning & Supra — in review

**Paper**: Ilin, Zmushko & Richtárik, *Super-Tuning: From Activation-Aware Pruning to Sparse Fine-Tuning* — [arXiv:2607.09287](https://arxiv.org/abs/2607.09287)
**Upstream PR**: [`huggingface/peft#3518`](https://github.com/huggingface/peft/pull/3518) — open (+1309 / −3, 24 files)
**Fork branch**: [`feat/supertuning-supra-magnitude`](https://github.com/smellslikeml/peft/tree/feat/supertuning-supra-magnitude)
**Coordination**: [`huggingface/peft#3450`](https://github.com/huggingface/peft/issues/3450) — direction confirmed by @BenjaminBossan
**Paper-author sign-off**: [`vectozavr/SuperTuning#3`](https://github.com/vectozavr/SuperTuning/issues/3) — Ivan Ilin (paper first author) is a co-author on the branch commits

Freezes the base weight and trains only a sparse support of scalar entries selected by weight magnitude — a distinct point in the trainable-parameter Pareto vs LoRA. Setting `r > 0` additionally allocates a LoRA-style low-rank adapter composed additively (the paper's Supra hybrid). Runs against `method_comparison/MetaMathQA/` produce numbers directly comparable to the harness's existing LoRA / SHiRA baselines.

![Super-Tuning & Supra — sparse ⊕ low-rank subspaces](figures/supertuning.png)

---

## 3. Scaling DoRA — internal, upstream pending license clarification

**Paper**: Zelenin & Zhuravlyova, *Scaling DoRA: High-Rank Adaptation via Factored Norms and Fused Kernels* — [arXiv:2603.22276](https://arxiv.org/abs/2603.22276)
**Internal PR**: [`smellslikeml/peft#18`](https://github.com/smellslikeml/peft/pull/18)
**Fork branch**: [`feat/dora-factored-kernel`](https://github.com/smellslikeml/peft/tree/feat/dora-factored-kernel)
**Kernel package**: [`remyxai/dora-factored-kernel`](https://huggingface.co/kernels/remyxai/dora-factored-kernel) on HF Hub
**Blocking**: [`sockeye44/dorafactors#1`](https://github.com/sockeye44/dorafactors/issues/1) — upstream reference has no LICENSE file

Two contributions bundled: (a) a factored-norm identity that replaces one `[d_out × d_in]` materialization with three small matmuls (~5% per-step speedup at r=2048 on Qwen-7B; 316× per-module intermediate reduction at the paper's r=64 regime); (b) a fused Triton kernel implementing DoRA's compose + norm as a single-pass GPU op.

![Scaling DoRA — factored norm decomposition](figures/dora_factored_norm.png)

---

## Composite analytics — PR #3382

![Composite PR #3382 analytics](figures/composite_pr3382_analytics.png)

Six panels of the merged Riemannian LoRA PR against the year's other merged tuner PRs: **size** (401 LOC, p82); **composition** (41% test / 48% src / 11% benchmark, above the cohort median's 13% test / 1% benchmark); **merge latency** (32.7 days, p89 — landed despite a first-time contributor); **scope shape** (focused + additive, no unrelated refactors); **author recurrence** (first merged PR to a repo where the top maintainer has 174); **feature-utility signals** (registry attachment, backwards-compat commitment, benchmark-harness registration under `method_comparison/MetaMathQA/`).

---

## PR sizing against the year's merged tuner PRs

Concrete comparison to `huggingface/peft`'s other tuner-adding PRs merged since 2025 (via `gh pr view <n>`):

| PR | Method | Merged | +Additions | −Deletions | Files |
|---:|--------|:------:|-----------:|-----------:|------:|
| [#2584](https://github.com/huggingface/peft/pull/2584) | SHiRA | 2025-07-14 | +1623 | −9 | 27 |
| [#2851](https://github.com/huggingface/peft/pull/2851) | GraLoRA | 2025-11-18 | +1238 | 0 | 20 |
| [#3037](https://github.com/huggingface/peft/pull/3037) | PSoFT | 2026-02-27 | +1556 | −1 | 23 |
| [#3084](https://github.com/huggingface/peft/pull/3084) | PEANuT | 2026-03-16 | +1096 | 0 | 23 |
| [#3195](https://github.com/huggingface/peft/pull/3195) | BEFT | 2026-04-30 | +773 | −5 | 22 |
| [**#3382**](https://github.com/huggingface/peft/pull/3382) | **Riemannian LoRA** | **2026-08-03** | **+401** | **−3** | **6** |
| [**#3518**](https://github.com/huggingface/peft/pull/3518) | **Super-Tuning & Supra** | in review | **+1309** | **−3** | **24** |

**#3382** landed at the compact end (401 LOC vs cohort median ~1150) — small, focused, all-additive. **#3518** sits in the cohort median (1309 LOC / 24 files) — same shape as PSoFT, PEANuT, GraLoRA.

---

## Outrider's role

Outrider surfaced all three candidates and drafted the fork branches. Human review shepherded each through coordination-issue-first upstreaming:

1. **Method discovery** — arXiv ↔ repo mapping identifies candidates whose mechanisms haven't been ported to the host repo.
2. **Draft on the fork** — Outrider produces an initial branch on `smellslikeml/peft` with tests, docs page, integration hooks.
3. **Human refinement + coordination** — coordination issue upstream (per PEFT's `CLAUDE.md`), maintainer feedback incorporated on the fork.
4. **Upstream PR** — filed against `huggingface/peft:main` referencing the coordination issue and, where applicable, paper-author sign-off.

The composite above shows the utility of this rhythm: a first-time contributor's PR can land through PEFT's review process when it arrives shaped the way the maintainer expects — focused, additive, test-heavy, with a durable feature-utility signal.

---

## References

- **PEFT contribution guide**: https://huggingface.co/docs/peft/main/en/developer_guides/contributing
- **PEFT `CLAUDE.md` (AI-assistance policy)**: https://github.com/huggingface/peft/blob/main/CLAUDE.md
- **Paper infographics** — the four diagrams were generated with [`llmsresearch/paperbanana`](https://github.com/llmsresearch/paperbanana).
