#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p outputs/reviews
review_prompt=$(mktemp)
trap 'rm -f "$review_prompt"' EXIT
cat > "$review_prompt" <<'EOF'
Perform an adversarial, read-only review of recon/, tests/, docs/official/POLICIES.md and FORMAT.md. Inspect available outputs/*report.json and outputs/*score.txt for evidence. Find correctness bugs, temporal balance errors, missing phase-opening metadata, allocation ambiguity, scalability bottlenecks, leakage and weak evaluation. Cite file and line, severity, evidence and concrete fix. Do not edit, submit, access credentials, or read outside this repository. Focus on material findings. The code is a hackathon solution, optimize reliability and measurable quality under a one-day deadline.
EOF
CLAUDE_CODE_EFFORT_LEVEL=xhigh claude -p --model claude-opus-5-5 --effort xhigh \
  --tools Read,Glob,Grep --allowedTools Read,Glob,Grep \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' --output-format json \
  < "$review_prompt" > outputs/reviews/code-review.json
