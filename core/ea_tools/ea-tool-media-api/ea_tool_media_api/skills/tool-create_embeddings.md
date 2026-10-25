---
name: tool-create-embeddings
description: "Produce vector embeddings for one string or a batch of strings via the active backend's embeddings model, for semantic search and clustering."
hidden: true
context: inline
---

# tool-create_embeddings

`create_embeddings` turns text into vectors using the configured provider's embedding model. Use it to build semantic indexes, cluster documents, detect near-duplicates, or score similarity between passages. The response includes a short sample of the first few dimensions per input; the full vectors live in the backend response, not in the tool output.

## When to Use

- Build a semantic search index over a document set.
- Group or cluster texts by meaning rather than keywords.
- Score similarity between a query and candidate passages.
- Deduplicate near-identical content before feeding a model.
- Embed a batch in one call instead of looping.

## When NOT to Use

- Chat completions or reasoning — use the normal model turn.
- Content safety screening — use `create_moderation`.
- Keyword/lexical search over files — use `grep`.
- Searching your own memory store — use `memory_search`.
- Exact-match or numeric similarity that does not need semantics.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `input` | string | yes | Text to embed, or a **JSON-encoded array** of strings for batch embedding. |

### Parameter Notes

- Batching is expressed by passing a JSON string that decodes to an array: `["a", "b"]` as the `input` value, not a native array.
- A single string embeds one item; the array form returns one vector per element.
- Output is truncated to the first 5 dimensions per item for display — do not compute similarity from the sample.
- Input token limits are the model's, not the tool's; oversized inputs error out.

## Example

```json
{
  "input": "[\"tool dispatch rules\", \"skill registry lookup\", \"prompt section assembly\"]"
}
```

## Best Practices

- Batch related texts in one call; it is cheaper and keeps vectors comparable.
- Keep each item well under the model's token limit; split long documents into chunks with overlap.
- Store the full vectors from the backend, not the truncated sample, if you need real similarity math.
- Keep chunking strategy stable across the corpus so distances stay meaningful.

## Common Pitfalls

- Passing a native JSON array instead of a JSON-encoded string fails validation.
- The 5-dimension sample looks similar for unrelated texts — it is not the embedding.
- Mixing models across calls makes distances incomparable; pin one model per index.
- Empty strings and whitespace-only items still consume a slot and produce degenerate vectors.

## Anti-patterns

- Using embeddings to answer factual questions directly (they only measure similarity).
- Recomputing an entire corpus for one new document instead of appending.
- Using embeddings where `grep` or a database index would do.

## Pairing with Other Tools

- `document` / `pdf` / `spreadsheet` — gather the corpus to embed.
- `memory_search` — semantic retrieval over remembered context.
- `database` — persist vectors alongside source rows.
- `create_moderation` — screen content before indexing it.
