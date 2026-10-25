---
name: travel-destination
description: "Destination discovery guidance - find attractions, POIs, neighborhoods, food, and what to do at a destination."
aliases: [destination, attractions, poi, things-to-do]
argument_hint: "[destination question: where, what kind of activity]"
user_invocable: true
hidden: true
---

## Destination Discovery Guidance

You are helping the user discover what to do at a destination: **{{args}}**

### When to Use
- Find attractions, landmarks, museums, parks at a destination
- Find restaurants, street food, markets, neighborhoods worth visiting
- Rank/prioritize what is worth the time vs skippable
- Find seasonal or event-based activities (festivals, seasonal scenery)

### When NOT to Use
- **Route/directions between two points** -> `travel-transit`
- **Multi-day itinerary sequencing** -> `travel-itinerary` (this skill is about discovery, not sequencing)
- **Hotel booking** -> `travel-hotels`
- **Visa** -> `travel-visa`

### Data Sources (no open API - search + fetch)
**International:**
- TripAdvisor (tripadvisor.com) - ranked attractions + reviews; the default for "top things to do"
- Google Maps POI (maps.google.com) - search a city for attractions/restaurants with ratings + hours
- Lonely Planet (lonelyplanet.com) - curated editorial picks
- Atlas Obscura (atlasobscura.com) - unusual / off-the-beaten-path sites
- Michelin Guide (guide.michelin.com) - restaurants for food-focused trips

**Domestic (China):**
- Dianping (dianping.com) - restaurants + local services with reviews; the default for food
- Mafengwo (mafengwo.cn) - attractions ranking + travelogues
- Xiaohongshu (xiaohongshu.com) - trendy / photogenic spots, recent real experience
- Meituan (meituan.com) - ticketed attractions + reviews
- Ctrip destination guides

### Search Workflow
1. **Clarify the interest** -> destination, what kind of activity (sightseeing / food / nature / shopping / nightlife / family), and any constraints (budget, accessibility, time available, must-see vs off-path). Ask if vague.
2. **Search ranking sources** -> `web_search` `<destination> top things to do` / `<> Must-Visit Attractions` (destination must-see attractions) then `web_fetch` TripAdvisor / Mafengwo / Google Maps POI. Collect a ranked list with rating + approx visit time + cost.
3. **For food specifically** -> Dianping (China) or Michelin/TripAdvisor (international); note the cuisine type and price tier, not just the name.
4. **Filter to the user's interests** -> 3-5 well-chosen picks beat 20 dumped names; explain why each fits the user's stated interest.
5. **Practical notes** -> opening hours, ticketed/advance-booking need, peak crowd times, best season, how long to budget.

### Rendering
- `info` tool `display: base` -> a destination card: grouped by category (Sights / Food / Shopping / Nightlife) with 2-3 picks each (name + rating + approx time + cost + one-line why).
- For a single deep-dive (e.g. "tell me about the Forbidden City"), a focused card with hours, tickets, route tips, and what not to miss.

### Best Practices
- Always include a rating + sample size when citing ("4.6/5, 3,200 reviews") so the user trusts the pick
- Note visit duration and opening hours - prevents "we got there and it was closed"
- For popular sites, flag advance-booking requirement (Forbidden City, Vatican, Anne Frank House)
- Mix a must-see with a lesser-known pick - gives the user both the highlight and something special
- For food, name the dish the place is known for, not just the restaurant

### Common Pitfalls
- **Dumping 20 names** -> the user cannot act on a wall of text; curate to 3-5
- **No practical info** -> "go to the Louvre" without "book timed entry, allow half a day, closed Tuesday"
- **Stale seasonal info** -> cherry blossoms / autumn leaves / festivals are date-sensitive; check the window
- **Ignoring the user's interest** -> recommending nightlife to a family traveler; filter to the stated interest
- **Tourist traps** -> some top-ranked spots are overpriced and crowded; flag alternatives when relevant

### Pairing with Other Tools
- `web_search` / `web_fetch` - all destination data
- `info` - render the destination card
- `travel-itinerary` - sequence the discovered POIs into a day plan
- `travel-transit` - how to get to each POI
- `travel-weather` - seasonal fit for outdoor activities
