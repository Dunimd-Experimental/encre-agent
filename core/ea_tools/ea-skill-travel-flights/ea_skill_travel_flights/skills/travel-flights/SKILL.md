---
name: travel-flights
description: "Flight search and booking guidance - find flights, compare fares, track prices, understand routes and layovers via aggregator sites."
aliases:
aliases: [flights, airline, flight-search]
argument_hint: "[flight search request: origin, destination, dates]"
user_invocable: true
hidden: true
---

## Flight Search Guidance

You are helping the user find and compare flights: **{{args}}**

### When to Use
- Find flights between an origin and destination on specific dates
- Compare fares across airlines / aggregators
- Find the cheapest month or flexible-date options
- Understand layovers, total travel time, baggage rules, or airline reputation

### When NOT to Use
- **Train travel** (especially China domestic) -> `travel-trains` (12306 / Trainline)
- **Hotels** -> `travel-hotels`
- **Destination info / what to do there** -> `travel-destination`
- **A single carrier's site for an already-known flight number** -> `web_fetch` that carrier directly

### Data Sources (no open API - search + fetch)
**International:**
- Google Flights (google.com/flights) - best for flexible-date calendar and price graph
- Skyscanner (skyscanner.com) - good for "whole month" / "cheapest month" and budget carrier aggregation
- Kayak (kayak.com) - price alerts and multi-city
- Google search for the route -> follow aggregator links

**Domestic (China):**
- Ctrip (ctrip.com) - dominant OTA, domestic + international
- Fliggy (fliggy.com) - Alibaba's OTA
- Qunar (qunar.com) - price-comparison focused

### Search Workflow
1. **Assume sensible defaults, search immediately.** Do not interrogate the user first. If the date is missing, check whether the request implies one ("this weekend", "next month"); if not, assume the nearest upcoming date or a flexible-date search and state the assumption. Only ask when a missing detail genuinely changes the result AND cannot be defaulted - and if you must ask, batch every such question in one message.
2. **Search the aggregator** -> use `web_search` with a query like `flights <origin> to <destination> <date> site:google.com/flights` or ` <> <> <> ` (flights <origin> to <destination> <date> Ctrip). Then `web_fetch` the result page. Do the search in the same turn as the request - never reply with "I'll search" without actually searching.
3. **Handle no-direct-flight cities (common for small origins like Yining).** Many city pairs have no direct flight. When results are thin or empty, do NOT report "no flights found" and stop - that is a failed delivery. Instead:
    - Search connecting flights via a hub (e.g. Urumqi, Xi'an, Chengdu for western China). Surface 1-stop options with the layover airport + total duration.
    - Check rail alternatives in parallel (`travel-trains`) - a train + flight combination is often the real answer for remote origins.
    - State the situation plainly: "no direct flight; here are 1-stop options and a train alternative".
4. **Parse the results** -> extract: airline, flight number, depart/arrive times, duration, stops + layover airports, price + currency, baggage allowance. Present as a table.
5. **Compare 2-3 options** -> don't just return one; surface the cheapest, the fastest, and a balanced option with the tradeoff stated.
6. **Deliver the full plan in one turn.** If the trip involves ground transport to the origin airport or from the destination airport, include those legs (delegate to `travel-transit`). The deliverable is the door-to-door journey, not the flight segment alone.

### Rendering
- Use the `info` tool with `display: base` to render a flight comparison card: a table of options (airline / depart-arrive / duration / stops / price) plus a short recommendation and a "book here" pointer.
- For a multi-leg journey (e.g. train + flight via hub), render one card covering all legs so the user sees the complete plan.

### Best Practices
- Always confirm dates are unambiguous (spell the month; watch MM/DD vs DD/MM confusion for international users)
- Note the timezone of depart/arrive times (local to each airport)
- Flag long layovers (>4h) and overnight layovers explicitly - they look cheap but cost a day
- State whether the price includes baggage; budget carriers often exclude it
- Give a booking link but remind the user to verify the final price on the OTA/airline site before paying

### Common Pitfalls
- **Asking before searching** -> the user said "fly from A to B"; search immediately, do not ask for date/budget one at a time. Assume defaults, state them, and deliver.
- **Stopping at "no direct flight found"** -> for small-city origins this is common; pivot to connecting flights via a hub and train alternatives. "No direct flight" is a finding, not a final answer.
- **Returning one flight and stopping** -> compare at least 2-3; the user wants choice
- **Ignoring timezone** -> a "10:00 depart" is meaningless without the airport's local tz
- **Trusting a stale cached price** -> aggregator prices shift; tell the user to reconfirm
- **Recommending without stating tradeoffs** -> cheapest may have 2 stops + 8h layover; say so
- **Booking on the model's say-so** -> never claim a price is final; prices are indicative until the OTA confirms
- **Delivering a fragment instead of the plan** -> "here are flights" without ground transport to/from the airport leaves the user stuck mid-journey

### Pairing with Other Tools
- `web_search` / `web_fetch` - primary path for all flight data
- `info` - render the comparison card
- `travel-visa` - check if the destination needs a visa before booking
- `travel-weather` - weather at the destination on arrival date
- `travel-destination` - what to do once there
