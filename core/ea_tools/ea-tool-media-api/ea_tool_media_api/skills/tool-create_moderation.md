---
name: tool-create-moderation
description: "Classify text against a provider moderation policy and return per-category flags and scores for harmful or unsafe content."
hidden: true
context: inline
---

# tool-create_moderation

`create_moderation` runs text through the active backend's moderation model. It returns a `flagged` verdict plus per-category scores (harassment, hate, self-harm, sexual, violence, and provider-specific categories). Use it to screen content before displaying, storing, or forwarding it — as one signal, not as the only gate.

## When to Use

- Screen user-generated text before it is published or persisted.
- Check model output before showing it to an audience.
- Batch-review a dataset of comments or messages for unsafe content.
- Add a safety signal to an automated pipeline.
- Compare content against a policy requirement before shipping.

## When NOT to Use

- Sentiment analysis, toxicity scoring for research, or opinion mining.
- PII/secret detection — moderation does not find those.
- Spam or phishing classification — different model.
- As the sole safety gate for high-stakes content; it needs human review.
- Profanity filtering in code or logs (rules are more predictable).

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `input` | string | yes | Text to classify for harmful or unsafe content. |

### Parameter Notes

- `input` is a single string; there is no batch array form, so loop for multiple items.
- The response contains `flagged` and per-category scores; thresholds vary by provider, so apply your own cutoff.
- Categories and their names differ across providers — do not hard-code a fixed set.

## Example

```json
{
  "input": "You are useless and nobody wants you here."
}
```

## Best Practices

- Decide a threshold explicitly and record it; a raw score is not a decision.
- Re-check the `flagged` array rather than assuming a single boolean covers all categories.
- Log the model/version used, since scores are not comparable across versions.
- For bulk screening, sample and calibrate before running the whole corpus.
- Escalate borderline scores to human review instead of silently dropping content.

## Common Pitfalls

- The backend must implement `create_moderation`; otherwise the call returns an unsupported error.
- Short or quoted text (sarcasm, quoted slurs in an article) is frequently misjudged.
- Category sets are provider-specific; code that assumes OpenAI's exact taxonomy breaks elsewhere.
- A low score is not a clearance — context, images and intent are invisible here.

## Anti-patterns

- Using moderation as a spam or PII filter.
- Auto-deleting content on a single category score with no review path.
- Treating it as a legal compliance certification.
- Sending an entire document when only the risky paragraph needs screening.

## Pairing with Other Tools

- `create_embeddings` — screen before indexing content for retrieval.
- `notify` — alert a channel when a batch trips the policy.
- `database` — persist verdicts with the content they describe.
- `document` — record policy decisions for auditing.
