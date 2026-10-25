---
name: tool-chart
description: "Render line/bar/scatter/pie/histogram/area/heatmap charts with matplotlib to a PNG file or an inline base64 image."
hidden: true
context: inline
---

# tool-chart

Renders a chart from data you already hold and returns either an inline base64 PNG or a file on disk. It is a static-image renderer, not a plotting environment: you describe the chart declaratively with `chart_type` + `data` and get pixels back.

## When to Use

- You have a small table or list in memory and need a static image for a report, slide deck, or chat reply.
- You want a quick visual sanity check on numbers (distribution, trend, correlation).
- The output must be embedded in a document (`document`, `presentation`, `spreadsheet`) as an image.
- You need a heatmap from a 2-D matrix with axis labels.

## When NOT to Use

- Interactive dashboards or hover tooltips — this renders a static image; use a plotting library in a `notebook` instead.
- Node-link / flow / architecture diagrams — use `diagram`.
- Very large datasets (tens of thousands of points): deduplicate or aggregate first, and save with `output_path` rather than returning base64.
- When a plain table communicates the numbers better than a chart (a handful of exact values).

## Key Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `chart_type` | string enum | yes | `line`, `bar`, `scatter`, `pie`, `histogram`, `area`, `heatmap`. |
| `title` | string | no | Chart title shown at the top. |
| `x_label` | string | no | X-axis label. |
| `y_label` | string | no | Y-axis label. |
| `data` | object | yes | The payload: `data.x` (x labels/values), `data.series` (list of `{name, values, color?, marker?, style?, alpha?, bins?}`), `data.matrix` (2-D list) plus `data.y_labels` for heatmaps. |
| `output_path` | string | no | Save path, e.g. `chart.png`. When omitted, the image is returned as base64. |
| `width` | number | no | Figure width in inches (default 10). |
| `height` | number | no | Figure height in inches (default 6). |
| `format` | string enum | no | `png` (default), `svg`, `pdf` — applies when saving. |
| `dpi` | integer | no | Resolution (default 100). |
| `bins` | integer | no | Histogram bin count (default 20). |

### Parameter Notes

- `data.series` is the multi-series path: one entry per line/bar group, each with its own `name` (legend), `values`, and optional styling.
- For `heatmap`, supply `data.matrix` (rows of numbers) and `data.y_labels` if the row labels are meaningful. `data.x` supplies column ticks.
- For `pie`, the label count in `data.x` must equal the value count in the series — a mismatch renders incorrectly or errors.
- `bins` can be set globally or per series inside `data.series`; the per-series value wins.
- `format` only matters with `output_path`; base64 output is always PNG.
- `dpi` multiplies the pixel size: 300 dpi at 10×6 inches is 3000×1800 px, which is heavy to inline.

## Example

```json
{
  "chart_type": "line",
  "title": "Tool calls per day",
  "x_label": "Day",
  "y_label": "Calls",
  "data": {
    "x": ["Mon", "Tue", "Wed", "Thu", "Fri"],
    "series": [
      { "name": "browser", "values": [12, 18, 9, 22, 30], "marker": "o" },
      { "name": "terminal", "values": [40, 35, 51, 47, 60], "marker": "s", "alpha": 0.8 }
    ]
  },
  "width": 10,
  "height": 6,
  "dpi": 120,
  "output_path": "reports/tool_calls.png"
}
```

Heatmap:

```json
{
  "chart_type": "heatmap",
  "title": "Error rate by hour",
  "data": {
    "x": ["00", "06", "12", "18"],
    "y_labels": ["Mon", "Tue", "Wed"],
    "matrix": [[0.1, 0.2, 0.5, 0.3], [0.0, 0.1, 0.4, 0.2], [0.3, 0.6, 0.9, 0.4]]
  }
}
```

## Best Practices

- Save to `output_path` whenever the chart is bigger than a thumbnail or you will reference it more than once; base64 inflates the transcript.
- Label axes and give every series a `name`; an unlabeled multi-series chart is unreadable in a report.
- Match the chart type to the question: trend → `line`, comparison → `bar`, distribution → `histogram`, share of a whole → `pie` (few slices), correlation → `scatter`, intensity over two dimensions → `heatmap`.
- Keep `dpi` at 100–150 for chat, 300 only for print-quality documents.
- Cap series count at ~5; more than that needs small multiples or aggregation.

## Common Pitfalls

- Pie charts with many slices or mismatched label/value counts.
- Passing raw strings for values; numeric series must be numbers, not `"12"`.
- Forgetting `data.x` and getting a default integer index that means nothing to the reader.
- Inlining a 300 dpi base64 image into chat and blowing the context budget.
- Assuming the tool reads a CSV/DataFrame from disk — it takes inline data only; load and preprocess first.
- Teaching the tool about missing values: `null`/`NaN` points may break a line series; clean or interpolate beforehand.

## Anti-patterns

- Rendering the same chart repeatedly while iterating on styling — vary one thing per call.
- Using a chart to show two numbers that belong in a sentence.
- Feeding a 50k-row time series directly; aggregate to a meaningful granularity first.
- Treating `chart` as a general plotting API (annotations, subplots, 3-D) — it is a fixed catalogue.

## Pairing with Other Tools

- `spreadsheet` — write the source table alongside the chart.
- `document` / `presentation` — embed the saved image in a report or slide.
- `diagram` — for flowcharts, architecture, and node-link graphics.
- `image` — to inspect or convert the rendered output.
- `notebook` — when you need full matplotlib control, subplots, or interactivity.
