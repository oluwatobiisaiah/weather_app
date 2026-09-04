# Weather Risk & Outdoor Activity Planner

A Tkinter desktop app. You type a place and pick an outdoor activity; it fetches the forecast,
decides whether the activity is **Safe**, **Manageable**, **Risky** or **Avoid**, tells you the best
time of day to do it, gives safety advice, builds a packing checklist, and remembers your favourite
locations and past searches.

Weather comes from [Open-Meteo](https://open-meteo.com) (no API key). The written explanation comes
from Gemini, and the app works completely without it.


---

## Running it

```bash
cd weather_app
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt

python main.py
```

No API key is needed to start. To turn on the AI wording:

```bash
copy .env.example .env            # cp on macOS/Linux
# then paste your key into GEMINI_API_KEY
```

### Command line

The same core, without the window, useful for checking a scoring change quickly:

```bash
python cli.py Ibadan football
python cli.py "Port Harcourt" picnic --date 2026-09-05 --save
python cli.py 7.3776, 3.9059 jogging      # coordinates skip geocoding
python cli.py --activities                 # list the activity keys
python cli.py Lagos farming --no-ai        # force the rule-based wording
```

### Tests

```bash
python -m pytest
```

178 tests, all offline, they drive the app from hand-built forecast payloads, so they run the same
with the network unplugged.

---

## How the verdict is decided

**Python decides; the AI explains.** This is the one rule the whole design hangs on.

Every forecast hour is scored on eight factors, heat, cold, rain, wind, UV, storm, visibility,
humidity. Each returns a penalty from 0 (perfect) to 100 (dangerous), using the thresholds for that
activity in [`data/activities.json`](data/activities.json). The penalties are combined, hard stops
are applied, and the result is a number from 0 to 100:

| Score | Band |
|---|---|
| 0–24 | **Safe**: conditions are good |
| 25–49 | **Manageable**: workable, but prepare |
| 50–74 | **Risky**: real precautions, or pick another time |
| 75–100 | **Avoid**: don't do this |

That band is then given to Gemini as a *fact*, and Gemini is asked to explain it. If the model
returns a different verdict, the computed one wins and the disagreement is logged. Turn the API key
off and the verdict, the timings, the advice and the checklist are all still there, only the wording
changes.

### Why the score is not a plain average

The first version used a straight weighted mean, and it was wrong. On a real Ibadan afternoon, feels
like 33.5 °C, 74% chance of rain by evening, six factors sitting at zero dragged a heat penalty of
100 down to an overall 18, and the app called it **Safe**.

So the score blends two things: the weighted mean (what the day is like overall) and the worst single
factor scaled by how much that activity cares about it (what will actually hurt you). Fog scoring 100
matters enormously for `travelling` and barely at all for `football`, and the scaling is what encodes
that. See the docstring in [`core/risk_analyzer.py`](core/risk_analyzer.py).

Some conditions bypass the arithmetic entirely. A thunderstorm floors the score at 80, gusts past the
activity's limit + 20 km/h floor it at 75, and so does an apparent temperature above 40 °C or below
−5 °C. Averages hide danger: one thunderstorm hour inside a pleasant afternoon must not average away.

### Tuning it

The difference between football and farming is data, not code. Farming treats a 60% chance of rain as
*ideal* where football tolerates 20%; a picnic weights rain at 5 where farming weights it at 1. Edit
`data/activities.json` and restart, no Python changes, and adding a new activity needs no UI change
either.

---

## What's where

```
weather_app/
├── main.py                   entry point (GUI)
├── cli.py                    entry point (command line)
├── services.py               builds the object graph for both
├── config.py                 paths, endpoints, tunables, logging
├── core/
│   ├── exceptions.py         the error hierarchy
│   ├── validators.py         every regex in the app
│   ├── models.py             Location, HourPoint, Forecast, ActivityPlan, …
│   ├── weather_client.py     Open-Meteo: geocode, forecast, retry, cache
│   ├── risk_analyzer.py      the scoring rules, the authoritative part
│   ├── recommendation.py     best windows, advice, packing list
│   ├── ai_client.py          Gemini: prompt, call, parse, validate
│   └── storage.py            favourites, history, plans, cache
├── ui/
│   ├── app.py                the controller and the threading
│   ├── widgets.py            verdict badge, risk meter, checklist
│   └── views/                search bar, sidebar, results
├── data/
│   ├── activities.json       activity thresholds and weights (edit me)
│   ├── favourites.json       ┐
│   ├── search_history.json   ├ created on first use
│   ├── plans/                │
│   └── cache/                ┘
├── logs/app.log              rotating, 1 MB × 3
└── tests/                    pytest, fully offline
```

Two rules keep the layout honest: **nothing in `core/` imports tkinter**, and **nothing in `ui/`
calls requests**. That is what lets the CLI and the test suite drive the entire product without a
window.

---

## When things go wrong

The app is built to keep working. What each failure looks like:

| What happened | What you see |
|---|---|
| Location field is empty or junk | "Enter a town or city name.", nothing is fetched |
| Place doesn't exist | "No place called 'Lagoss' was found. Check the spelling." |
| No internet | Your last saved forecast, with the status bar saying how old it is |
| Service is slow | Two automatic retries, then "The weather service is slow to respond." |
| Rate limited (429) | "Too many requests just now, try again in a minute." |
| Gaps in the forecast data | Nothing, the affected factor is dropped from the score |
| No API key, or Gemini is down | Rule-based wording, and one quiet note in the status bar |
| `favourites.json` got damaged | "…was damaged and has been reset." The old copy is kept as `favourites.corrupt-<timestamp>` |

Anything genuinely unexpected shows a short dialog and writes the full traceback to `logs/app.log`.
The user never gets a traceback; the log never gets a shrug.

### Try it yourself

```bash
# invalid input
python cli.py "!!!" football

# no such place
python cli.py Lagoss picnic

# offline behaviour: disconnect the network, then re-run a search you have
# already done once, you get the cached forecast, marked as cached

# corrupt file recovery
echo not json > data/favourites.json
python main.py                     # tells you once, then carries on
```

---

## Notes

- **Timezones.** Open-Meteo is called with `timezone=auto`, so every timestamp is local to the place
  you searched, a 15:00 in the timeline is 15:00 *there*. Don't compare those against
  `datetime.now()` without converting.
- **Threading.** Tkinter widgets are only ever touched from the main thread. Network calls run on a
  worker and hop back with `self.after(0, …)`; see `PlannerApp._post`.
- **Forecast range.** Seven days. Dates outside that window are refused before any request is made.
- **The key.** `.env` is gitignored. The key travels in the `x-goog-api-key` header rather than the
  URL, because query strings end up in logs.
