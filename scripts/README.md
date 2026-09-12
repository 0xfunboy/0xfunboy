# Profile metrics

`update-profile-metrics.py` builds the statistics, language, streak and activity
SVGs displayed in the profile README. It uses Python's standard library.

```sh
python3 scripts/update-profile-metrics.py --username 0xfunboy --output-dir assets
```

Set `GH_TOKEN` to increase the rate limit for GitHub's public REST endpoints.
The token is never sent to the contribution-calendar page.

The **Refresh Profile Visuals** workflow runs daily, on profile changes and on
manual dispatch. It generates the cards alongside the contribution snake and
publishes them to the `output` branch. The README loads those generated images;
the copies in `assets` are the initial design snapshots.

## What the numbers mean

- Repository statistics use owned, public repositories and identify fork exclusions.
- Language composition measures GitHub-reported code bytes in public repositories
  excluding forks. It describes the codebase, rather than time spent or proficiency.
- Contributions come from the anonymous public profile calendar. GitHub may include
  anonymized private contribution counts when the profile owner makes them public;
  private repository information is never fetched.
- Streaks are calculated within the displayed calendar range. A day still in
  progress with no contributions does not break a streak ending yesterday.
- The activity chart uses the last 90 calendar days in that same data.

Malformed or incomplete source data fails generation. The workflow publishes only
after generation succeeds, preserving the previous output if a refresh fails.
