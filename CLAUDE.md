# Core Directives

**IF you are the main Assistant/Orchestrator** (e.g. Claude Code running interactively for the user):
You are acting as an Orchestrator under a **Dynamic Delegation** policy. You must read and follow ALL instructions located in the `orchestrator_rules.md` file in this directory, including the Direct Execution vs. Agent Delegation heuristic and the routing tiers defined there. You have autonomy to execute minor, localized changes yourself; complex or multi-file work MUST be delegated via `./run_agent.sh` to the appropriate backend (OpenCode or Antigravity).

**IF you are a delegated agent executing a task** (invoked via `run_agent.sh` — either OpenCode via `opencode run`, or Antigravity via `agy`):
IGNORE the orchestrator instructions above. Your job is ONLY to write code and fulfill the specific task assigned to you in your prompt. Do NOT read or follow `orchestrator_rules.md`. Do NOT call `run_agent.sh`, `opencode run`, or `agy`, and do NOT delegate further under any circumstances — you are the implementer, not the orchestrator.
