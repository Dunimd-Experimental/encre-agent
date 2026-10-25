---
name: travel-visa
description: "Visa and entry requirements guidance - check visa policy, required documents, e-visa eligibility, passport validity."
aliases:
aliases: [visa, entry-requirements, passport]
argument_hint: "[visa question: passport country, destination, trip purpose]"
user_invocable: true
hidden: true
---

## Visa & Entry Requirements Guidance

You are helping the user understand visa/entry requirements: **{{args}}**

### When to Use
- Check whether a visa is required for a passport holder visiting a destination
- Find e-visa / visa-on-arrival eligibility
- Understand required documents (passport validity, photos, proof of funds, onward ticket)
- Find where/how to apply and typical processing time

### When NOT to Use
- **Booking the flight/train** -> `travel-flights` / `travel-trains` (but check visa first!)
- **What to do at the destination** -> `travel-destination`
- **An already-decided application status** -> the user should check the embassy/application portal directly

### Data Sources (no open API - search + fetch)
**International aggregators:**
- iVisa (ivisa.com) - visa requirement checker + e-visa application service; good for "do I need a visa" lookup by passport + destination
- Sherpa (joinsherpa.com) - travel restrictions + visa eligibility checker
- VisaHQ (visahq.com) - requirement lookup by nationality/destination
- KAYAK travel restrictions - high-level entry rules

**Official sources (authoritative, slower to parse):**
- Destination country's embassy/consulate website
- Destination country's foreign-affairs ministry
- For China outbound: National Immigration Administration (NIA) and the destination embassy in China

**Domestic (China) specifics:**
- Chinese passport holders: many destinations now visa-free or e-visa (Thailand, Malaysia, Singapore, UAE, etc. - policies change frequently, verify)
- 144-hour transit visa-free policy for China as a destination

### Search Workflow
1. **Confirm the key facts** -> passport nationality, destination country, trip purpose (tourism / business / transit), duration of stay, any prior visas for the destination. These determine the answer.
2. **Check the aggregator first** -> `web_search` `<passport> passport visa for <destination>` then `web_fetch` iVisa / Sherpa / VisaHQ for a quick eligibility answer.
3. **Verify with the official source** -> visa policies change; cross-check the embassy/foreign-affairs site for the current rule. Cite both.
4. **If a visa is required** -> document: visa type, required documents (passport validity - usually 6 months beyond stay, photo specs, application form, fee, proof of accommodation/onward travel), processing time, where to apply (embassy / online / on arrival).
5. **If visa-free / e-visa / VoA** -> state the conditions (max stay, purpose limits, port-of-entry restrictions) so the user knows the boundary.

### Rendering
- `info` tool `display: base` -> a requirements card: passport + destination + verdict (visa-free / e-visa / visa required) + (if required) the application checklist (documents, fee, processing time, where to apply) + a "verify before travel" note.
- For a simple visa-free case, a compact card stating the max stay + conditions.

### Best Practices
- Always state the passport nationality in your answer - the same destination has different rules per passport
- Flag passport validity (6 months beyond stay is the common rule; some require more)
- Note that visa policies change frequently - always tell the user to verify with the official source before booking non-refundable travel
- For transit, distinguish "transit without visa" (TWOV) rules - they depend on layover duration and whether you leave the airport
- If the user is applying, give the official application link, not a third-party reseller (unless the user wants the concierge service)

### Common Pitfalls
- **Answering without the passport nationality** -> the answer depends entirely on it; ask first
- **Treating an aggregator as gospel** -> policies lag; verify with official source before advising "go ahead and book"
- **Confusing e-visa with visa-free** -> e-visa still requires an application and approval; it is not the same as walking in
- **Ignoring stay-duration limits** -> "visa-free" usually caps at 15/30/90 days; exceeding it is an overstay offense
- **Old transit-visa info** -> TWOV policies shift (China's 144h policy has expanded/changed); check current scope

### Pairing with Other Tools
- `web_search` / `web_fetch` - all visa data
- `info` - render the requirements card
- `travel-flights` - check visa before booking a non-refundable flight
- `travel-itinerary` - visa may limit trip duration or routing
