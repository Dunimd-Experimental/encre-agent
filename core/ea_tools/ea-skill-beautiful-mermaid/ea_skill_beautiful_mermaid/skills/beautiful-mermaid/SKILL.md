---
name: beautiful-mermaid
description: Write clean Mermaid source and render it to PNG or SVG — flowcharts, sequence, state, class, ER, Gantt, and Mermaid's full diagram catalog.
aliases:
  - mermaid
  - mermaid-diagram
when_to_use: "mermaid diagram flowchart sequence diagram"
argument_hint: "[what the diagram should show]"
user_invocable: true
context: inline
---

# Mermaid Diagrams

Mermaid is the diagram language here: you write text, the `diagram` tool renders it to an image. It is the only dialect guaranteed to work with no extra installation — Graphviz needs `dot`, PlantUML needs `plantuml`.

## When to Use

- Flowcharts, decision flows, process maps.
- Sequence diagrams between services or agents.
- State machines, class diagrams, ER diagrams, Gantt charts, pie charts, journey maps.
- A visual for a document, slide, README, or PR description.
- Keeping the DSL in version control so a human can edit it later.

## When NOT to Use

- Quantitative data — bars, lines, distributions → `chart`.
- Freehand illustration, icons, photos → `image` / `generate_image`.
- Diagrams without a text DSL (hand-drawn mind maps, whiteboard photos).
- Live GUI canvas editing — this is a batch renderer.
- Node-link graphs that need a real layout engine for hundreds of nodes.

## Diagram Types

| Kind | Mermaid block | Use for |
|---|---|---|
| Flowchart | `flowchart TD` / `LR` | Processes, decisions, pipelines |
| Sequence | `sequenceDiagram` | Calls between actors over time |
| State | `stateDiagram-v2` | Lifecycle, state machines |
| Class | `classDiagram` | Object structure |
| ER | `erDiagram` | Database entities and relations |
| Gantt | `gantt` | Timelines and milestones |
| Journey | `journey` | User steps with satisfaction |
| Git graph | `gitGraph` | Branch history |
| Mindmap / timeline | `mindmap`, `timeline` | Hierarchies, chronology |

## Tools You Use

| Task | Tool | Key params |
|---|---|---|
| Render to image | `diagram` | `action=generate`, `diagram_type=mermaid`, `source`, `output_path`, `theme`, `width` |
| Save DSL only | `diagram` | `action=save_source`, `diagram_type`, `source`, `output_path` |
| Keep DSL in the repo | `file_write` | `.mmd` next to the rendered image |
| Iterate on the source | `file_read`, `file_edit` | edit, then re-render |
| Embed the result | `document`, `presentation` | reference the saved path |
| Check the pixels | `image` | verify dimensions or content |
| Render outside the tool | `terminal` | `mmdc` (mermaid-cli) |

### Parameter Notes

- `diagram_type` must match the syntax: a DOT string sent as `mermaid` fails with a parse error, not a hint.
- `theme` is Mermaid-only: `default`, `dark`, `neutral`, `forest`, `base`.
- `width` is pixels, Mermaid-only; `0` means auto.
- Mermaid renders with `mmdc` when installed, otherwise it falls back to the public `mermaid.ink` service — **Mermaid content may leave the machine**. Say so when the diagram holds sensitive data.
- Rendering times out at 30–60s. On failure the tool may fall back to saving the source only, so read the returned message to know whether an image exists.

## Workflows

### Render a diagram

1. Write the Mermaid source; pick `TD` for vertical flows, `LR` for left-to-right pipelines.
2. Quote labels that contain punctuation: `A["(optional step)"]`.
3. `diagram` → `action=generate` with `output_path`.
4. Read the returned message: if it says "saved source", no image was produced — fix the DSL and retry.
5. Verify the file with `file_read` or `image`.

### Iterate safely

`action=save_source` first while shaping the structure, `action=generate` only once the DSL is stable. Keep the `.mmd` in the repo so the human can change one line later.

### Diagram for a report

1. Render to `output_path` at a width that matches the target container.
2. Save the `.mmd` beside it.
3. Reference the image path from `document` or `presentation`.

## Examples

```json
{
  "action": "generate",
  "diagram_type": "mermaid",
  "theme": "neutral",
  "output_path": "docs/images/skill-flow.png",
  "source": "flowchart TD\n  A[User turn] --> B[Prompt build]\n  B --> C{Tool guide loaded?}\n  C -- no --> D[Skill index]\n  C -- yes --> E[Tool Skills section]\n  D --> F[Tool call]\n  E --> F"
}
```

```json
{
  "action": "save_source",
  "diagram_type": "mermaid",
  "output_path": "docs/diagrams/loop-sequence.mmd",
  "source": "sequenceDiagram\n  User->>Loop: message\n  Loop->>Router: classify\n  Router-->Loop: tools\n  Loop->>Tool: execute\n  Tool-->Loop: result\n  Loop-->>User: reply"
}
```

## Best Practices

- Match `diagram_type` to the syntax exactly.
- Keep diagrams directional and small — more than ~15 nodes means split it.
- Use `\n` consistently as line separators inside `source`; do not mix tabs and semicolons.
- Use `save_source` first, then render once.
- Check the return message; "saved source" after a failed render looks like success if you only check for a path.
- Pick `theme: dark` for dark backgrounds and `neutral` for documents.

## Common Pitfalls

- Assuming an image exists after a timeout — it may have fallen back to source only.
- Sending DOT as Mermaid and reading the parse error as a tool outage.
- Unquoted labels containing parentheses or colons breaking the parser.
- Rendering the same diagram at every step instead of once at the end.
- Expecting `theme` or `width` to affect Graphviz or PlantUML.
- Shipping a diagram that no one can read at display size.

## Anti-patterns

- Diagramming data that a table or chart communicates better.
- Generating an image when the user asked for editable source.
- Building one giant architecture graph instead of a few focused ones.
- Embedding sensitive internal names in a diagram rendered through the public fallback service.

## Pairing with Other Tools

- `document` / `presentation` — embed the rendered image.
- `chart` — quantitative visuals instead of structural ones.
- `image` — post-process or inspect the output.
- `file_write` — keep the DSL next to the generated image.
- `terminal` — `mmdc` when you need local, offline rendering.
