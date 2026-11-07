# Team branches

`main` contains shared preparation and the agreed solution only. Work in your personal branch:

| Contributor | Branch |
|---|---|
| Vitalii | `solution/vitalii` |
| Luke | `solution/lukemininno` |
| Marina | `solution/marinamoncayola` |
| eltriki-X | `solution/eltriki-x` |

Other personal branches start from shared preparation, independently of Vitalii's implementation. Vitalii's solver is in `solution/vitalii` and can be inspected without merging it.

After cloning, run `git fetch origin` and `git switch --track origin/solution/YOUR_BRANCH`. If the local branch already exists, use `git switch solution/YOUR_BRANCH`.

Open a pull request into `main` when ready. Include reproducible commands, input phase, score, runtime, memory and limitations. Compare on the same data and scorer. Sample-development scores and temporal tests with oracle opening balances are not official LIVE scores. Merge the agreed solution only after team review; do not merge automatically based on a single score.

Keep tokens, bulk datasets and generated outputs out of Git. Do not force-push shared branches. One person operates competition submissions, especially the single FINAL submission.
