# Version Matrix

| Component | Pin / Version | Notes |
|---|---|---|
| Metabase Image | `metabase/metabase:v0.54.3` | Official Docker distribution matching DB schema v54 |
| PostgreSQL Image | `postgres:16-alpine` | Application DB engine matching `metabase_envdata.sql` pg_dump version |
| Gemini Model | `gemini-2.5-computer-use-preview` | Google Computer Use model |
| Playwright | `^1.49.0` | Browser control runtime |
| Python | `>=3.10` | Benchmark runner runtime |
