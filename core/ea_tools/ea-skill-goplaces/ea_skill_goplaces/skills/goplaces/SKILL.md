---
name: goplaces
description: "Look up places (restaurants, cafes, shops, hotels) via Google Places API and the goplaces CLI: search nearby, ratings, hours, reviews, resolve names to coordinates. Needs GOOGLE_PLACES_API_KEY."
---

# goplaces

A command-line front end to Google Places API (New): text search, place
resolution and place details, with human-readable output by default and
`--json` for parsing.

## When to Use

- "What's a good coffee shop near me / near this address?"
- The user asks for ratings, review counts, opening hours, price level or address of a specific venue.
- You need to turn a place name ("Soho, London") into coordinates or a `place_id`.
- You are building an itinerary and need real venues with verified details.

## When NOT to Use

- Flight, train or hotel *booking* with dates and fares — use the `travel-flights`, `travel-trains` or `travel-hotels` skills.
- Route planning and travel time between two points — that is a maps/directions service, not Places.
- Business data enrichment at scale (hundreds of records) — the API is billed per request; confirm with the user first.
- Any lookup where a general `web_fetch` of an official page is enough.

## Prerequisites

- `GOOGLE_PLACES_API_KEY` must be exported in the environment. If it is missing, say so and stop — do not invent results.
- Optional `GOOGLE_PLACES_BASE_URL` for a proxy or test endpoint.
- The `goplaces` binary on `PATH` (`brew install steipete/tap/goplaces` on macOS).

Run everything through the `terminal` tool.

## Workflow

1. Read the request for constraints: location, category, open-now, rating floor, budget, count.
2. Search with those constraints rather than searching generically and filtering afterwards:

   ```
   goplaces search "coffee" --open-now --min-rating 4 --limit 5
   ```

3. If the user gave an area rather than coordinates, bias the search to it:

   ```
   goplaces search "pizza" --lat 40.8 --lng -73.9 --radius-m 3000
   ```

4. Fetch details (hours, phone, reviews) only for the one or two candidates that matter:

   ```
   goplaces details <place_id> --reviews
   ```

5. Present a short ranked list with the facts that drove the ranking; name the source (Google Places).

## Example

```
goplaces resolve "Soho, London" --limit 5
goplaces search "sushi" --lat 51.513 --lng -0.136 --radius-m 800 --min-rating 4.2 --limit 5
goplaces details ChIJN1t_tDeuEmsRUsoyG83frY4 --reviews --json
```

## Key Options

| Option | Meaning |
|---|---|
| `--limit <n>` | Max results returned |
| `--open-now` | Only places open at request time |
| `--min-rating <r>` | Rating floor (0–5) |
| `--lat` / `--lng` / `--radius-m` | Location bias and search radius in metres |
| `--type <type>` | Category filter; **only the first value is sent** (API limit) |
| `--page-token <t>` | Fetch the next page of an existing search |
| `--reviews` | Include reviews in details output |
| `--json` | Machine-readable output |
| `--no-color` | Disable ANSI colour (also honours `NO_COLOR`) |

Price levels reported by the API run `0..4` (free → very expensive).

## Best Practices

- Convert a vague area into coordinates with `goplaces resolve` before searching.
- Ask for 3–5 candidates, then fetch details for the winner only — every call is billed.
- Prefer `--json` when you will post-process or compare fields.
- Quote prices, ratings and hours exactly as returned; never round or estimate.

## Common Pitfalls

- Sending three `--type` values and assuming all three were applied.
- Treating an empty result set as "nothing exists" — retry with a wider radius or drop `--open-now` first.
- Ignoring `next_page_token` and concluding the list is short.
- Presenting a place name without its address: two venues often share a name.

## Anti-patterns

- Inventing venues, ratings or hours when the API key is missing or the call fails.
- Scraping Google Maps HTML as a workaround.
- Searching the whole city for "dinner" and handing back an unfiltered dump.
