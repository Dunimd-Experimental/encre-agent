---
name: chart-visualization
description: Choose the right chart and render it — trend, comparison, distribution, share, correlation, intensity, plus a matplotlib escape hatch for everything else.
aliases:
  - data-viz
  - chart
  - visualization
when_to_use: "chart graph plot visualize data-viz"
argument_hint: "[the data, or a file, plus what you want to show]"
user_invocable: true
context: inline
---

# Chart Visualization

Turn numbers into a picture that answers one question. The primary path is the `chart` tool — a declarative static renderer with a fixed catalogue (`line`, `bar`, `scatter`, `pie`, `histogram`, `area`, `heatmap`). When the question needs something outside that catalogue, drop to `notebook` or `terminal` with matplotlib.

## When to Use

- You have a table or list in hand and need a static image for a report, slide, or chat reply.
- You want a visual sanity check: trend, distribution, correlation, hotspot.
- The visual must be embedded in a `document`, `presentation`, or `spreadsheet`.
- A heatmap over a 2-D matrix with axis labels.

## When NOT to Use

- Interactive dashboards, hover tooltips or brushing — this is a static image; use `notebook`.
- Flow, sequence, architecture or ER diagrams — use `diagram`.
- Freeform illustration, icons, photos — use `image` or `generate_image`.
- A handful of exact values — a sentence or a table communicates better than a chart.
- Data you do not have yet: load and clean first (`spreadsheet`, `notebook`), this tool takes inline data only.

## Chart Type Selection

| Question | `chart` type | Notes |
|---|---|---|
| How does it change over time/order? | `line` | One line per series, `data.x` = steps |
| Which is bigger, by how much? | `bar` | Cap at ~5 series; more needs small multiples |
| What is the shape of the data? | `histogram` | `bins` global or per series (per-series wins) |
| How does one part compare to the whole? | `pie` | Few slices only; label count must equal value count |
| Do two variables move together? | `scatter` | Numeric on both axes |
| Cumulative / stacked magnitude? | `area` | Readable with few stacked series |
| Intensity over two dimensions? | `heatmap` | `data.matrix` + `data.x` / `data.y_labels` |

## Tools You Use

| Task | Tool | Key params |
|---|---|---|
| Static chart | `chart` | `chart_type`, `data`, `title`, `x_label`, `y_label` |
| Save instead of inlining | `chart` | `output_path`, `format` (`png`/`svg`/`pdf`), `dpi` |
| Full matplotlib control | `notebook` | subplots, annotations, 3-D |
| Scripted render | `terminal` | matplotlib / seaborn script |
| Source data | `spreadsheet` | `action=read` |
| Embed in a report | `document`, `presentation` | reference the saved image path |
| Inspect / convert output | `image` | check or resize the rendered file |

### Parameter Notes

- `data.series` is the multi-series path: one entry per line/bar group with `name` (legend), `values`, and optional `color`, `marker`, `style`, `alpha`, `bins`.
- `data.matrix` plus `data.x` / `data.y_labels` are the heatmap path.
- `format` only applies when `output_path` is set; base64 output is always PNG.
- `dpi` multiplies pixel size: 300 dpi at 10×6 in is 3000×1800 px.
- Values must be numbers, not `"12"`.

## Workflows

### Render for a report

1. Load the data (`spreadsheet` / `notebook`) and pick the chart type from the question, not the data.
2. `chart` with `output_path`, axes labels, and a `name` for every series.
3. `dpi` 100–150 for chat, 300 only for print.
4. Embed the saved path in the document and verify the file exists.

### Quick sanity check in chat

Omit `output_path` to get base64. Keep `dpi` at 100 and the dataset small; a large inline image blows the context budget.

### Anything beyond the catalogue

`notebook` or `terminal`: seaborn heatmaps, groupby subplots, annotations, error bars, 3-D, small multiples. Then `file_write` the image next to the report.

## Examples

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

- Match the chart to the question; do not default to bars.
- Label axes and name every series — an unlabeled multi-series chart is unreadable in a report.
- Save to `output_path` whenever the chart is bigger than a thumbnail or referenced twice.
- Cap series at ~5; aggregate or use small multiples beyond that.
- Give `data.x` real labels; a default integer index means nothing to a reader.

## Common Pitfalls

- Pie charts with many slices, or mismatched label/value counts.
- Passing strings for numeric values.
- Forgetting `data.x` and shipping a meaningless integer axis.
- Inlining a 300 dpi base64 image into chat.
- Assuming the tool reads a CSV from disk — it takes inline data only.
- Feeding `null` / `NaN` points into a line series; clean or interpolate first.

## Anti-patterns

- Rendering the same chart repeatedly while tweaking style — vary one thing per call.
- Charting two numbers that belong in a sentence.
- Feeding a 50k-row time series without aggregating.
- Treating `chart` as a general plotting API (annotations, subplots, 3-D) — it is a fixed catalogue.

## Pairing with Other Tools

- `spreadsheet` — write the source table alongside the chart.
- `document` / `presentation` — embed the saved image.
- `diagram` — flowcharts, architecture, node-link graphics.
- `notebook` — full matplotlib control and interactivity.
- `image` — inspect or convert the rendered output.
