---
name: travel-hotels
description: "Hotel and lodging search guidance - compare prices, read reviews, find short-stay rentals via aggregators."
aliases:
aliases: [hotels, lodging, accommodation]
argument_hint: "[hotel search request: destination, check-in/out dates, guests]"
user_invocable: true
hidden: true
---

## Hotel Search Guidance

You are helping the user find and compare lodging: **{{args}}**

### When to Use
- Find hotels / hostels / short-stay rentals for a destination and dates
- Compare prices across aggregators for the same property
- Read reviews and assess location quality vs price
- Decide between hotel, hostel, and apartment/Airbnb by trip type

### When NOT to Use
- **Flights / trains to get there** -> `travel-flights` / `travel-trains`
- **What to do at the destination** -> `travel-destination`
- **A single hotel's own site for an already-chosen booking** -> `web_fetch` that site directly

### Data Sources (no open API - search + fetch)
**International:**
- Booking.com - largest inventory, good for price + free-cancellation filter
- Agoda - strong in Asia, sometimes cheaper for the same property
- Hotels.com - has a "stay 10 get 1 free" loyalty angle
- Airbnb - apartments / whole homes / long stays
- Hostelworld - hostels and budget dorms
- TripAdvisor - reviews aggregator (cross-source)

**Domestic (China):**
- Ctrip (ctrip.com) - dominant, hotel + reviews
- Fliggy (fliggy.com) - Alibaba OTA
- Meituan (meituan.com) - strong for budget/business hotels and domestic reviews
- Tujia (tujia.com) - China's Airbnb equivalent

### Search Workflow
1. **Confirm params** -> destination city + area preference, check-in date, check-out date (compute nights), guest count + room count, budget range, must-haves (free wifi, breakfast, parking, pet-friendly, near a specific station/landmark).
2. **Search aggregators** -> `web_search` `hotels in <area> <check-in> <check-out>` or `<> <Check-in> <> ` (destination hotel check-in check-out Ctrip), then `web_fetch` the listing page.
3. **Parse the results** -> property name, star rating, guest score, price/night + total, location/distance to a landmark, key amenities, free-cancellation flag. Present as a table.
4. **Cross-check 2 aggregators** -> the same property is often priced differently on Booking vs Agoda vs Ctrip; surface the cheapest for the same property.
5. **Read reviews** -> if the user is deciding between 2-3, fetch the TripAdvisor / Meituan review summary and cite the common praise + complaints.

### Rendering
- `info` tool `display: base` -> a table of 3-5 options (name / stars / score / price-per-night / total / location / key amenity) + a recommendation.
- For a shortlist of 2, a side-by-side comparison card (price + pros + cons).

### Best Practices
- Compute total cost (price/night x nights + taxes + fees), not just nightly rate
- Note the cancellation policy - "free cancellation until X" is a real value, especially for uncertain plans
- Location matters more than stars: a 3-star next to the station often beats a 5-star an hour out
- For longer stays (7+ nights), check Airbnb / serviced apartments - usually cheaper than hotels
- State the guest score source and sample size ("8.4/10 from 1200 reviews") so the user trusts it

### Common Pitfalls
- **Showing nightly rate only** -> hides taxes/fees; always show total
- **One aggregator only** -> prices differ across OTAs for the same property; compare
- **Ignoring location** -> a cheap hotel far from everything costs time and transit money
- **Forgetting cancellation terms** -> non-refundable looks cheaper but is risky for flexible plans
- **Recommending without review context** -> a cheap 4-star with a 6.0 score is a warning, not a deal

### Pairing with Other Tools
- `web_search` / `web_fetch` - all hotel data
- `info` - render the comparison card
- `travel-transit` - distance from hotel to stations/attractions
- `travel-destination` - what to do near the hotel
- `travel-weather` - seasonal packing based on destination weather
