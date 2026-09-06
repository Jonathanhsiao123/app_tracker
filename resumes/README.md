# Resume variants (spec 9)

Four hand-written variants, authored once, checked in as PDFs. `analyze/variants.py` scores
a posting and recommends one; you attach the PDF by hand when you apply.

| File | Variant | Leads with |
|---|---|---|
| `generalist.pdf` | `generalist` | Broad SWE: coursework, projects, general engineering |
| `mle.pdf` | `mle` | Adversarial robustness honors research, ML coursework |
| `systems.pdf` | `systems` | Lenahan Lab spectrometer instrument software, PyQt6, hardware control |
| `security.pdf` | `security` | ML-based IDS for IoT, adversarial threat model |

**The keyword profiles do not live here.** Build step 0 moved them to
`jobmon/config/variants.yaml` and dropped the per-variant term lists entirely: a variant is
now a set of per-taxonomy-category weights, so scoring works with no PDF on disk. Point each
`pdf:` key at the file below once it exists — until then it stays `null`, which is legal and
must never raise.

**TODO:** write the four documents. Four hours, once. This replaces per-job generation
entirely — the ceiling on per-job rewriting is low with ~4 substantive items to reorder,
and keyword-driven rewriting drifts into stuffing, which reads as machine-written.

Every weighted category must exist in `taxonomy.yaml`, or it silently scores nothing.
`load_variants(taxonomy=...)` warns when it does not.
