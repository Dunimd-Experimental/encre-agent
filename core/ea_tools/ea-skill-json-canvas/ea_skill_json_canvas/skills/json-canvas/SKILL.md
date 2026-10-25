---
name: json-canvas
description: Create and edit JSON Canvas files (.canvas) — nodes, edges, groups, colors, and connections — for mind maps, flowcharts, project boards, and Obsidian canvases.
aliases:
  - canvas
  - obsidian-canvas
  - mindmap-canvas
when_to_use: ".canvas"
argument_hint: "[canvas file path or the structure to build]"
user_invocable: true
hidden: true
auto_activate: true
context: inline
---

## JSON Canvas (.canvas)

Build and edit `.canvas` files following the [JSON Canvas Spec 1.0](https://jsoncanvas.org/spec/1.0/). A canvas is a plain JSON document with two top-level arrays:

```json
{
  "nodes": [],
  "edges": []
}
```

## When to Use

- Creating mind maps, flowcharts, process diagrams, or project boards as a `.canvas` file.
- Adding nodes, groups, or connections to an existing canvas.
- Laying out related notes, files, and links in Obsidian.

## When NOT to Use

- **Rendered diagram images (PNG/SVG)** — use the `diagram` tool (Mermaid, Graphviz, PlantUML).
- **Free-form vector artwork or a designed poster** — use the `canvas-design` skill.
- **A simple list or outline** — a Markdown file is clearer than a canvas.

## Tools You Use

| Task | Tool | Key params |
|------|------|------------|
| Read the existing canvas | `file_read` | `path` |
| Write the canvas | `file_write` | `path`, `content` |
| Validate JSON and IDs | `terminal` | `command` — `python -c "import json; ..."` |

## Node Attributes

Array order is z-index: first node is at the bottom, last is on top.

| Attribute | Required | Type | Description |
|-----------|----------|------|-------------|
| `id` | Yes | string | Unique 16-character lowercase hex id |
| `type` | Yes | string | `text`, `file`, `link`, or `group` |
| `x` | Yes | integer | X position in pixels (can be negative) |
| `y` | Yes | integer | Y position in pixels |
| `width` | Yes | integer | Width in pixels |
| `height` | Yes | integer | Height in pixels |
| `color` | No | color | Preset `"1"`-`"6"` or hex, e.g. `"#FF0000"` |

### Text nodes

```json
{
  "id": "6f0ad84f44ce9c17",
  "type": "text",
  "x": 0,
  "y": 0,
  "width": 400,
  "height": 200,
  "text": "# Hello World\n\nThis is **Markdown** content."
}
```

Use `\n` for line breaks. A literal `\\n` renders as the characters backslash and `n`.

### File nodes

`file` is required (path to a file), `subpath` is optional (heading or block link starting with `#`).

```json
{
  "id": "a1b2c3d4e5f67890",
  "type": "file",
  "x": 500,
  "y": 0,
  "width": 400,
  "height": 300,
  "file": "Attachments/diagram.png"
}
```

### Link nodes

`url` is required.

```json
{
  "id": "c3d4e5f678901234",
  "type": "link",
  "x": 1000,
  "y": 0,
  "width": 400,
  "height": 200,
  "url": "https://obsidian.md"
}
```

### Group nodes

Visual containers for organizing other nodes. Place children inside the group bounds, with 20-50px padding.

| Attribute | Required | Type | Description |
|-----------|----------|------|-------------|
| `label` | No | string | Group title |
| `background` | No | string | Path to a background image |
| `backgroundStyle` | No | string | `cover`, `ratio`, or `repeat` |

```json
{
  "id": "d4e5f6789012345a",
  "type": "group",
  "x": -50,
  "y": -50,
  "width": 1000,
  "height": 600,
  "label": "Project Overview",
  "color": "4"
}
```

## Edge Attributes

| Attribute | Required | Default | Description |
|-----------|----------|---------|-------------|
| `id` | Yes | - | Unique identifier |
| `fromNode` | Yes | - | Source node id |
| `fromSide` | No | - | `top`, `right`, `bottom`, `left` |
| `fromEnd` | No | `none` | `none` or `arrow` |
| `toNode` | Yes | - | Target node id |
| `toSide` | No | - | `top`, `right`, `bottom`, `left` |
| `toEnd` | No | `arrow` | `none` or `arrow` |
| `color` | No | - | Line color |
| `label` | No | - | Text on the edge |

```json
{
  "id": "0123456789abcdef",
  "fromNode": "6f0ad84f44ce9c17",
  "fromSide": "right",
  "toNode": "a1b2c3d4e5f67890",
  "toSide": "left",
  "toEnd": "arrow",
  "label": "leads to"
}
```

## Colors

Preset values are intentionally undefined by the spec; applications substitute their own brand colors.

| Preset | Color |
|--------|-------|
| `"1"` | Red |
| `"2"` | Orange |
| `"3"` | Yellow |
| `"4"` | Green |
| `"5"` | Cyan |
| `"6"` | Purple |

## IDs

Generate 16-character lowercase hex strings (a 64-bit random value):

```
6f0ad84f44ce9c17
a3b2c1d0e9f8a7b6
```

## Layout Guidelines

- Coordinates may be negative; the canvas is infinite. `x` increases right, `y` increases down; position is the top-left corner.
- Space nodes 50-100px apart; align to a 10 or 20px grid for cleaner layouts.

| Node type | Suggested width | Suggested height |
|-----------|-----------------|------------------|
| Small text | 200-300 | 80-150 |
| Medium text | 300-450 | 150-300 |
| Large text | 400-600 | 300-500 |
| File preview | 300-500 | 200-400 |
| Link preview | 250-400 | 100-200 |

## Workflow

### Create a new canvas

1. Start from `{"nodes": [], "edges": []}`.
2. Generate unique 16-char hex ids for every node.
3. Add nodes with `id`, `type`, `x`, `y`, `width`, `height` and the type-specific field.
4. Add edges referencing valid node ids.
5. Validate before finishing (checklist below).

### Add a node to an existing canvas

1. Read and parse the file.
2. Generate an id that collides with no existing node or edge id.
3. Pick `x`/`y` that avoids overlaps, leaving 50-100px of space.
4. Append the node; add edges if it connects to other nodes.
5. Validate id uniqueness and edge references.

### Connect two nodes

1. Identify source and target node ids.
2. Generate a unique edge id and set `fromNode`/`toNode`.
3. Set `fromSide`/`toSide` for clean anchors and a `label` if the relationship needs a word.
4. Validate that both references exist.

### Edit an existing canvas

1. Read and parse the file.
2. Locate the node or edge by `id`.
3. Change the attributes in place.
4. Write the file back and re-validate.

## Validation Checklist

Run this on every write:

1. All `id` values are unique across both `nodes` and `edges`.
2. Every `fromNode` and `toNode` references an existing node id.
3. Required fields are present: `text` for text nodes, `file` for file nodes, `url` for link nodes.
4. `type` is one of `text`, `file`, `link`, `group`.
5. `fromSide`/`toSide` are `top`, `right`, `bottom`, or `left`.
6. `fromEnd`/`toEnd` are `none` or `arrow`.
7. Colors are presets `"1"`-`"6"` or valid hex.
8. The JSON parses.

```
python -c "import json,collections;d=json.load(open('board.canvas',encoding='utf-8'));ids=[n['id'] for n in d['nodes']]+[e['id'] for e in d['edges']];dups=[i for i,c in collections.Counter(ids).items() if c>1];refs={n['id'] for n in d['nodes']};bad=[e['id'] for e in d['edges'] if e['fromNode'] not in refs or e['toNode'] not in refs];print('dup ids:',dups,'dangling edges:',bad)"
```

If validation fails, check for duplicate ids, dangling edge references, and malformed JSON strings — unescaped newlines in node text are the most common cause.

## Best Practices

- Generate all ids in one place so they cannot collide.
- Lay out left to right or top to bottom and align to a grid.
- Put related nodes inside a `group` node so the canvas stays readable as it grows.
- Keep node text short; link to a note for anything long.
- Keep the JSON pretty-printed with 2-space indentation for hand editing.

## Common Pitfalls

- **Literal `\\n` in text** — renders as backslash and `n`; use a real `\n`.
- **Dangling edge references** — an edge to a node id that does not exist breaks the file.
- **Duplicate ids** — ids must be unique across nodes and edges together.
- **Overlapping nodes** — without 50-100px of spacing the canvas looks like a pile.
- **Huge nodes** — a 2000px text node crowds everything else; split long content into several nodes.
- **Inventing node types** — only `text`, `file`, `link`, and `group` are valid.

## References

- [JSON Canvas Spec 1.0](https://jsoncanvas.org/spec/1.0/)
- [JSON Canvas repository](https://github.com/obsidianmd/jsoncanvas)
- `references/EXAMPLES.md` — full examples: mind maps, project boards, research canvases, flowcharts.

## Pairing with Other Tools

- `diagram` — Mermaid, Graphviz, and PlantUML for rendered diagram images.
- `canvas-design` — designed posters and visual art.
- `obsidian-markdown` / `obsidian-cli` — notes that canvas nodes link to.
