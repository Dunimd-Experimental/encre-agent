---
name: travel-weather
description: "Travel weather guidance - destination forecast, best season to visit, what to pack, severe-weather awareness."
aliases:
aliases: [weather, forecast, packing]
argument_hint: "[weather question: destination, dates]"
user_invocable: true
hidden: true
---

## Travel Weather Guidance

You are helping the user with weather for travel: **{{args}}**

### When to Use
- Get the forecast for a destination on specific trip dates
- Determine the best season to visit a destination
- Advise on what to pack given the forecast and activities
- Flag severe weather (typhoon, blizzard, heatwave) that could disrupt travel

### When NOT to Use
- **Booking the trip** -> `travel-flights` / `travel-hotels` (but check weather to pick dates)
- **What to do at the destination** -> `travel-destination`
- **Current local weather small-talk** -> a quick `web_search` suffices; this skill is for travel-relevant decisions

### Data Sources
**Open API (prefer when available):**
- OpenWeatherMap (openweathermap.org) - free-tier forecast API; usable via `rest_client` with a key, or `web_fetch` the page
- WeatherAPI (weatherapi.com) - free tier, good forecast
- QWeather (qweather.com / Hefeng Weather) - China-focused, free developer tier; good for Chinese cities

**Web (search + fetch):**
- AccuWeather (accuweather.com) - 15-day + long-range seasonal
- Weather.com (weather.com) - forecast + radar
- China Weather (weather.com.cn / China Weather Network) - authoritative for China
- TimeAndDate (timeanddate.com/weather) - clean forecast tables, good for parsing

### Search Workflow
1. **Confirm what matters** -> destination, trip dates (or "best season to visit"), what the user will do (outdoor sightseeing needs a different bar than museum-hopping), and where they are coming from (packing acclimatization).
2. **Get the forecast** -> for specific dates within ~14 days: `web_fetch` OpenWeatherMap / AccuWeather / QWeather. For "best season": search climatological averages by month.
3. **Parse** -> daytime highs/lows, precipitation chance, wind, humidity, sunrise/sunset (matters for planning daylight hours), and any severe-weather alerts.
4. **Translate to decisions** -> "pack a light rain shell + layers", "expect 35C afternoons, plan indoor activities midday", "sunrise 06:20, start early to beat crowds".
5. **Flag disruption risk** -> typhoon season (China coast / Japan / SE Asia summer), blizzard (northern winters), monsoon rain - these can cancel flights/close sites.

### Rendering
- `info` tool `display: base` -> a weather card: destination + dates + a small day-by-day table (date / high-low / condition / precip / wind) + a packing line + any severe-weather note.
- For "best season" questions, a card with a month-by-month average (temp / rain / crowd level) + a recommended window.

### Best Practices
- Always give the temperature unit the user expects (ask if unsure; default Celsius internationally, Fahrenheit for US)
- Note sunrise/sunset for sightseeing-heavy trips - daylight is a planning constraint
- Connect weather to the itinerary: "rain in the afternoon -> schedule the museum then"
- For outdoor activities (hiking, beaches), wind + precipitation matter more than temperature
- Cite the source and the forecast's time horizon - beyond 7-10 days forecasts are unreliable; say so

### Common Pitfalls
- **Unit confusion** -> giving Fahrenheit to a Celsius user or vice versa; confirm
- **Forecasting too far out** -> a 25-day forecast is mostly noise; treat >10 day as indicative only
- **Ignoring severe weather** -> a typhoon landing during the trip can cancel everything; flag it
- **No packing translation** -> "30% precip" means nothing without "bring a shell"; translate to action
- **Treating averages as the forecast** -> climatological averages say what's typical, not what will happen on specific dates

### Pairing with Other Tools
- `rest_client` / `web_fetch` - fetch forecast (API if you have a key, else page)
- `info` - render the weather card
- `travel-itinerary` - weather shapes the day sequencing (indoor vs outdoor)
- `travel-destination` - outdoor POIs are weather-sensitive
- `travel-flights` - severe weather may disrupt flights
