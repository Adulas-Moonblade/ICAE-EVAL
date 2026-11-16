"""ICAE-Bench automated evaluation harness.

Drives Claude Code, OpenHands, or Codex CLI as the agent-under-test over a
PRD-only Docker container, then scores the generated code with objective
pass-rate metrics. See harness/orchestrator.py for the entry point.
"""
