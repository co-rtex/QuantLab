# Autonomous execution

QuantLab is being built in long Claude Code sessions. Work must survive context resets.

## Durable state
`STATUS.md` (handoff), `ROADMAP.md` (what next), `DECISIONS.md` (why), `CLAUDE.md`
(how to work here), plus Git history. Every milestone ends with tests, a commit, a push
and a STATUS update.

## Resuming
```bash
cd ~/Developer/QuantLab
git status && git log --oneline -15
cat STATUS.md ROADMAP.md
claude --continue        # or: claude -p "Resume QuantLab from STATUS.md" --permission-mode acceptEdits
```

## Supervision
A single interactive session is the primary mechanism. No recursive or self-spawning
Claude processes are used. If an external supervisor is added later it must run one
session at a time, use documented non-interactive flags, apply bounded retries, and stop
on repeated no-progress cycles.
