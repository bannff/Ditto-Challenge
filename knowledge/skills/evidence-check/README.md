# Evidence-check skill

This package supports the Verify stage. It exists to make the verification swarm inspect the actual diff and test evidence rather than repeat the implementation swarm’s claims.

`SKILL.md` requires concrete citations such as changed hunks, test results, and exit codes, then asks for a clear correct/not-correct recommendation. The workflow—not this skill—runs the authoritative acceptance command and decides whether a change may ship.
