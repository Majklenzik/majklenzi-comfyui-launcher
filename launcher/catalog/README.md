# Workflow catalog

Every `*.json` file in this folder is one card on the **Workflows** page. A file may hold one object or a
list of objects. Files whose name starts with `_` are ignored. Your own entries can also live on the volume in
`/workspace/.launcher/catalog/` (same format, they survive image updates and override entries with the same `id`).

| field | meaning |
|---|---|
| `id` | lowercase letters, digits, dashes. Unique. |
| `title`, `description`, `tags`, `note` | text shown on the card |
| `workflow_url` | direct https link to the workflow `.json` (for example a GitHub raw link), **or** |
| `workflow_file` | path of a `.json` next to the catalog file (for workflows you ship inside the image) |
| `workflow_name` | file name used inside ComfyUI's Workflows panel (default `<id>.json`) |
| `nodes` | GitHub links, or `{ "url": ..., "branch": ... }` |
| `models[].url` | direct download link. Hugging Face `blob` links are converted to `resolve` automatically |
| `models[].dir` | folder inside `ComfyUI/models`, for example `loras`, `diffusion_models`, `vae`, `text_encoders` |
| `models[].filename` | optional, taken from the link when it ends in a model file name |
| `models[].size_gb` | optional, only used to show the download size |
| `models[].gated` | `true` shows a "needs Hugging Face token" hint |
| `models[].sha256` | optional, exactly 64 hex characters. The file is checked after download and deleted when it does not match |

An entry may contain only nodes, only models, or only a workflow. A card with a broken field is skipped and the
reason is shown at the top of the Workflows page.

Installing a card again overwrites a workflow file of the same name, so keep your own edits under another name.
