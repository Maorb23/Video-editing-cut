# Semantic analysis adapters

No provider is required. Store optional artifacts as JSON and reference their
relative paths from `edit-plan.json`.

Transcript 1.0 contains `version: "1.0"` and `segments`. Each segment has
`start_frame`, `end_frame`, `text`, and optional speaker/confidence/language.

Visual analysis 1.0 contains `version: "1.0"` and `observations`. Each
observation has `frame`, `description`, and optional labels/regions/confidence.

Frames use the fixed project profile. Adapters may be manual, local, or hosted,
but their output must be materialized before deterministic compilation.

