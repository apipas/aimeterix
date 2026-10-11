# 0003: Language

- Status: accepted
- Date: 2026-10-09
- Plan ref: D0.3

## Context
AI teams mostly work in Python; the data, GPU (NVML) and database libraries we need are mature there.

## Decision
**Python 3.10+** for the whole project: collectors, engines, storage connectors and interfaces.

## Consequences
- Easy for the target users to read, extend and contribute to.
- The hardware agent carries a Python runtime. If its footprint becomes a real problem, a small agent in Go can be added later behind the same storage interface.
