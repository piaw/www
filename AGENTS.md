# AGENTS.md

- Coaching brief report dates and data windows use `America/Los_Angeles` local
  calendar dates.
- Coaching brief activity summaries use local Garmin activity JSON files in
  `data/activities`; Intervals.icu is used for wellness and training-load data.
- Missing or stale activity data means readiness is unknown. Never interpret an
  empty Garmin activity window as rest or display "Fresh / race-ready".
- Garmin and Intervals behavior must be tested with mocks or local fixture
  files; do not hit live Garmin or Intervals accounts in tests.
