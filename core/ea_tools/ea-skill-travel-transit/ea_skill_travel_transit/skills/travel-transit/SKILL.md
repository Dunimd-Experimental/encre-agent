---
name: travel-transit
description: "Local transit and navigation guidance - city metro, buses, taxi/ride-hail, walking directions, real-time traffic."
aliases:
aliases: [transit, navigation, directions, metro]
argument_hint: "[transit request: from A to B, mode, city]"
user_invocable: true
hidden: true
---

## Local Transit Guidance

You are helping the user navigate within a city: **{{args}}**

### When to Use
- Find directions between two points in a city (walking / transit / driving / cycling)
- Understand metro/subway lines, bus routes, transfer points
- Estimate travel time + fare for a specific mode
- Real-time traffic or disruption info

### When NOT to Use
- **Intercity flights** -> `travel-flights`
- **Intercity trains** -> `travel-trains`
- **Hotel booking** -> `travel-hotels`
- **Where a destination's attractions are** -> `travel-destination` (POI discovery, not routing)

### Data Sources (no open API - search + fetch)
**International:**
- Google Maps (maps.google.com) - directions by mode, transit schedules, street view, traffic
- Citymapper (citymapper.com) - excellent for metro/bus in major cities, real-time
- Transit App (transit.app) - similar to Citymapper
- Moovit - transit directions, strong in some regions

**Domestic (China):**
- Amap / Gaode (amap.com) - dominant for driving, transit, and ride-hail in China
- Baidu Maps (map.baidu.com) - strong transit + driving, alternative to Amap
- Tencent Maps (map.qq.com) - WeChat-integrated, third option

### Search Workflow
1. **Confirm the request** -> origin address/landmark, destination address/landmark, city, preferred mode (walk / transit / drive / cycle), departure time (or "now"). Ask if any are ambiguous - "the station" is not enough in a city with several.
2. **Search the right map** -> International: `web_fetch` the Google Maps directions URL. China: `web_search` ` <> <> ` (Amap <origin> to <destination> transit) or construct an Amap/Baidu query. Use `web_search` when a direct URL is hard to build.
3. **Parse the route** -> for the chosen mode: total time, distance, step-by-step (walk to X station -> take line Y -> transfer at Z -> ...), fare, number of transfers, real-time disruption notes.
4. **Offer 1-2 alternatives** -> transit often has a "fastest" and a "fewest transfers" option; surface both with the tradeoff.
5. **Flag practical details** -> last train time, weekend schedule differences, accessibility (elevators), paid vs free transfer areas.

### Rendering
- `info` tool `display: base` -> a route card: chosen mode + total time/distance/fare + numbered steps + a short note. For multi-mode comparison, a small table (mode / time / fare / transfers).
- For a simple "how do I get from A to B", a compact step list card.

### Best Practices
- Always state total time + fare + number of transfers for transit
- Note the mode of each segment (walk / metro line N / bus N) explicitly
- For China, prefer Amap/Baidu - Google Maps is unreliable for live transit there
- For driving, note real-time traffic and toll costs (China has tolls on most expressways)
- State the last-train / first-bus time for late-night or early-morning trips

### Common Pitfalls
- **Ambiguous endpoints** -> "the airport" in a city with multiple; confirm which
- **Using Google Maps for China transit** -> data is stale/incomplete; use Amap/Baidu instead
- **Ignoring transfers** -> a "25 min" route with 3 transfers is worse than a "30 min" direct; show transfers
- **Forgetting last train** -> a 23:30 trip may be impossible by metro; flag it
- **Stale schedules** -> transit schedules change; tell the user to verify in-app before relying

### Pairing with Other Tools
- `web_search` / `web_fetch` - all transit data
- `info` - render the route card
- `travel-hotels` - hotel-to-attraction routing
- `travel-destination` - what the destination POI is (routing assumes you know where)
- `travel-trains` / `travel-flights` - getting to the city before navigating within it
