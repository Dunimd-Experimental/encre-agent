---
name: tool-diagram
description: "Render Mermaid, Graphviz/DOT, or PlantUML source into PNG/SVG, or save the DSL for later rendering."
hidden: true
context: inline
---

# tool-diagram

Turns diagram-as-code into an image. You supply the DSL, pick the dialect with `diagram_type`, and the tool invokes the matching renderer. Mermaid is the only dialect guaranteed to work out of the box; Graphviz and PlantUML need their CLIs installed.

## When to Use

- Flowcharts, sequence diagrams, state machines, ER diagrams, class diagrams.
- Architecture or dependency graphs where you want deterministic, version-controllable source.
- A visual to embed in a document, slide, or PR description.
- Save the DSL (`action: "save_source"`) so a human can edit it later.

## When NOT to Use

- Data charts (bars, lines, distributions) — use `chart`.
- Freehand illustration, icons, or photo editing — use `image`.
- Live editing in a GUI canvas; this is a batch renderer.
- Hand-drawn mind maps or anything without a text DSL.

## Key Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `action` | string enum | yes | `generate` (render to an image) or `save_source` (write the DSL text only). |
| `diagram_type` | string enum | no | `mermaid` (default), `graphviz`, `plantuml` — must match the syntax you wrote. |
| `source` | string | yes | The DSL source: Mermaid, DOT, or PlantUML. |
| `output_path` | string | no | Output image (or source) path. Defaults to a temp/working-directory file. |
| `width` | integer | no | Output width in pixels; Mermaid only. `0` = auto. |
| `theme` | string enum | no | Mermaid theme: `default`, `dark`, `neutral`, `forest`, `base`. |

### Parameter Notes

- `source` is required for both actions; `save_source` does not validate the DSL, it only writes it.
- Mermaid renders with `mmdc` when installed, otherwise it falls back to the public `mermaid.ink` service — meaning Mermaid content may leave the machine.
- Graphviz needs `dot`; PlantUML needs `plantuml` (usually plus Java). Missing CLIs fail rather than degrade.
- Rendering has a 30–60s timeout. On failure the tool may fall back to saving the source, so always read the returned message to know whether an image exists.
- `width` and `theme` are ignored by Graphviz and PlantUML.

## Example

```json
{
  "action": "generate",
  "diagram_type": "mermaid",
  "theme": "neutral",
  "output_path": "docs/images/skill-flow.png",
  "source": "flowchart TD\n  A[User turn] --> B[Prompt build]\n  B --> C{Tool guide loaded?}\n  C -- no --> D[Skill index]\n  C -- yes --> E[Tool Skills section]\n  D --> F[Tool call]\n  E --> F"
}
```

Sequence diagram in PlantUML:

```json
{
  "action": "generate",
  "diagram_type": "plantuml",
  "output_path": "docs/images/loop-sequence.svg",
  "source": "@startuml\nUser -> Loop: message\nLoop -> Router: classify\nRouter --> Loop: tools\nLoop -> Tool: execute\nTool --> Loop: result\nLoop -> User: reply\n@enduml"
}
```

Keep the source without rendering:

```json
{
  "action": "save_source",
  "diagram_type": "graphviz",
  "output_path": "docs/diagrams/runtime.dot",
  "source": "digraph G { gateway -> harness; harness -> core; core -> native; }"
}
```

## Best Practices

- Match `diagram_type` to the syntax exactly; a DOT string sent as `mermaid` fails with a parse error, not a helpful hint.
- Keep diagrams small and directional. If it needs more than ~15 nodes, split it.
- Use `save_source` first when iterating on structure, then `generate` once the DSL is stable.
- Verify the returned message: "saved source" after a failed render looks like success if you only check for a path.
- For Mermaid, quote labels containing punctuation (`A["(optional)"]`) to avoid parser breakage.

## Common Pitfalls

- Assuming an image was produced when the renderer timed out and fell back to source.
- Using PlantUML/Graphviz on a machine without the CLI.
- Sending Mermaid with tabs/semicolons that the parser rejects; use `\n` separators consistently.
- Rendering the same diagram at every step instead of once at the end.
- Expecting `theme` to affect non-Mermaid dialects.

## Anti-patterns

- Diagramming data that a table or chart would communicate better.
- Generating an image when the user asked for editable source.
- Embedding huge architecture graphs that no one can read at display size.
- Treating a parse error as a tool outage; fix the DSL.

## Pairing with Other Tools

- `document` / `presentation` — embed the rendered image.
- `chart` — quantitative visuals as opposed to structural ones.
- `image` — post-process or convert the output.
- `file_write` — keep the DSL in the repo next to the generated image.
