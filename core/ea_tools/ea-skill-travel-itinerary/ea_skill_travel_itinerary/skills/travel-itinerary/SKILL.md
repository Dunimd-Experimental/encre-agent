---
name: travel-itinerary
description: "Trip planning guidance - build multi-day itineraries, estimate logistics and pacing, combine transport + stays + activities."
aliases:
aliases: [itinerary, trip-plan, travel-plan]
argument_hint: "[trip planning request: destination, duration, interests]"
user_invocable: true
hidden: true
---

## Trip Planning Guidance

You are helping the user plan a trip: **{{args}}**

### When to Use
- Build a day-by-day itinerary for a destination
- Estimate travel time and logistics between activities
- Balance pacing (not overpacking days, leaving buffer)
- Suggest an order of activities that minimizes backtracking

### When NOT to Use
- **A single booking** (one flight / one hotel / one train) -> the specific `travel-*` skill
- **Directions for one trip** -> `travel-transit`
- **Visa eligibility** -> `travel-visa`
- **Weather** -> `travel-weather`

### Data Sources (no open API - search + fetch)
**International:**
- Rome2Rio (rome2rio.com) - multi-modal transport between any two points; shows flights/train/bus/drive options with time + price estimates. Excellent for "how do I get from A to B" in a trip plan.
- TripAdvisor (tripadvisor.com) - attractions, ranking, reviews; good for what to do
- Lonely Planet (lonelyplanet.com) - curated destination guides
- Google Travel (google.com/travel) - high-level trip planning

**Domestic (China):**
- Mafengwo (mafengwo.cn) - user travelogues + guides, strong for domestic + popular intl
- Xiaohongshu (xiaohongshu.com) - visual guides, trendy spots, real recent experience
- Qyer (qyer.com) - budget-focused international travel guides
- Ctrip / Fliggy destination guides

### Search Workflow
1. **Confirm the trip skeleton** -> destination(s), duration (nights), traveler type (solo/couple/family), budget tier, interests (food / history / nature / shopping / nightlife), pace preference (packed vs relaxed). Ask if any are missing.
2. **Research activities** -> `web_search` `<destination> things to do` / `<> ` (destination must-see attractions guide), `web_fetch` TripAdvisor / Mafengwo / Xiaohongshu results. Collect a candidate list with location + approx time + cost.
3. **Cluster by area** -> group activities that are near each other to minimize transit; assign one cluster per day.
4. **Sequence for logistics** -> use Rome2Rio / Amap to estimate travel time between activities; order them to minimize backtracking. Leave buffer (meals, rest, transport delays).
5. **Produce the plan** -> day-by-day: morning / afternoon / evening blocks, with the activity, location, approx time, transport to next, and a meal suggestion.

### Rendering
- `info` tool `display: base` -> an itinerary card: per day, a table or list of (time / activity / location / notes). Include the destination + dates as a title.
- For multi-destination trips, a card with a route summary (city A -> city B -> city C with transport between).

### Best Practices
- Never pack more than 3-4 major activities per day; travel fatigue is real
- Always estimate transit time between activities, not just activity duration
- Leave one flexible/blank slot per day for rest or spontaneity
- For multi-city trips, sequence cities to minimize total travel (avoid zig-zag)
- Note opening hours and closed days (many museums close Monday; temples may close on certain days)
- Suggest booking timed-entry tickets in advance for popular sites (Louvre, Forbidden City, etc.)

### Common Pitfalls
- **Overpacking a day** -> 6 attractions in a day is a march, not a trip; cut to 3-4
- **Ignoring transit time** -> two attractions "30 min apart" eats an hour round-trip
- **No buffer** -> a single delay cascades; leave slack
- **Backtracking** -> visiting east-side and west-side sites on the same morning; cluster instead
- **Missing closed days** -> arriving to find the main museum shut; check hours first

### Pairing with Other Tools
- `web_search` / `web_fetch` - all planning data
- `info` - render the itinerary card
- `travel-flights` / `travel-trains` - intercity legs in the plan
- `travel-hotels` - where to base each night
- `travel-transit` - intra-city movement between activities
- `travel-destination` - deeper info on specific POIs
- `travel-weather` - seasonal/arrival-day weather to shape the plan
