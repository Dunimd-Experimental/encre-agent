---
name: travel-trains
description: "Train and rail search guidance - find train tickets, schedules, fares, passes via official and aggregator sites."
aliases:
aliases: [trains, railway, rail-pass]
argument_hint: "[train search request: origin, destination, date]"
user_invocable: true
hidden: true
---

## Train Search Guidance

You are helping the user find and compare train travel: **{{args}}**

### When to Use
- Find train schedules, fares, and ticket availability between two stations
- Compare high-speed vs conventional rail (time vs price)
- Understand rail passes (Eurail, Japan Rail Pass, etc.) vs point-to-point tickets
- China domestic rail booking (12306) - very common request

### When NOT to Use
- **Flights** -> `travel-flights`
- **Long-distance buses** -> usually `travel-transit` or `web_search`; rail skill focuses on trains
- **City metro/subway navigation** -> `travel-transit` (Citymapper / Amap)
- **Hotels at the destination** -> `travel-hotels`

### Data Sources (no open API - search + fetch)
**China domestic (rail is the dominant mode):**
- 12306 (12306.cn / China Railway) - the official source; authoritative for schedules, fares, and real-time availability
- Ctrip / Fliggy / Qunar rail sections - aggregator UI on top of 12306 data, sometimes easier to read

**Europe:**
- Trainline (trainline.eu) - aggregator across European operators, good UX
- Eurail / Interrail (eurail.com) - rail passes for multi-country travel
- National carriers: SNCF (France), DB (Germany), Trenitalia (Italy), Renfe (Spain), OBB (Austria)

**Japan:**
- JR (japanrailpass.net) - Japan Rail Pass info
- Hyperdia / Jorudan - schedule and fare lookup (search + fetch)

**Other:**
- Amtrak (amtrak.com) - USA
- National rail sites for India (IRCTC), UK (National Rail), Russia (RZD)

### Search Workflow
1. **Confirm params** -> origin station, destination station, date, ticket class (seat/sleeper/2nd-class/1st-class), passenger count. For China, confirm whether the user has a 12306 account (booking requires real-name ID).
2. **Search the right source** -> China: `web_search` `12306 <Departure> <> <>` (12306 <origin> to <destination> <date>) then `web_fetch`. Europe: Trainline or the national carrier. Japan: Hyperdia/Jorudan.
3. **Parse the results** -> train number, depart/arrive station + time, duration, stops, fare + class, seat availability. Present as a table.
4. **Compare classes** -> high-speed (G/D trains in China, Shinkansen, TGV) vs conventional (K/T/Z in China); state the time-price tradeoff.
5. **Rail pass evaluation** -> if multi-city travel, compute whether a pass beats point-to-point: roughly compare pass-per-day cost vs typical point-to-point fare for the planned legs.

### Rendering
- `info` tool `display: base` -> a table of train options (number / depart-arrive / duration / stops / fare / class) plus a recommendation.
- For rail-pass decisions, a card comparing "pass total cost" vs "sum of point-to-point fares".

### Best Practices
- Always specify station names fully - many cities have multiple stations (Beijing has Beijing South / Beijing West / Beijing North; London has many terminals)
- For China high-speed (G/D), note the second-class vs business-class price gap
- Flag that 12306 booking requires real-name verification + a Chinese ID or passport; foreign travelers can register but it is friction-heavy
- For sleeper trains, note the berth type (hard/soft, upper/lower) - it affects price and comfort
- State the ticket refund/change rules briefly - rail tickets often have tiered refund fees

### Common Pitfalls
- **Ambiguous station names** -> "Beijing station" is not enough; use the full name (Beijing South, etc.)
- **Conflating high-speed and conventional** -> a "cheap" K-train may take 3x as long; always show duration
- **Forgetting real-name booking** -> in China you cannot just buy and ride; the ticket is tied to an ID
- **Recommending a pass without math** -> a Eurail pass only pays off above a threshold of travel; compute it
- **Stale availability** -> 12306 availability is real-time; tell the user to book promptly when seats are scarce

### Pairing with Other Tools
- `web_search` / `web_fetch` - all train data
- `info` - render the comparison card
- `travel-flights` - compare train vs flight for the same route (trains often win under ~5h)
- `travel-hotels` - lodging at the destination
- `travel-transit` - getting between the station and the hotel
