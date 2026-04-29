---
description: Deep analysis and investigation of code, bugs, or architecture
argument-hint: "<target: file path, bug, or topic>"
---

Spawn an **advisor agent** to analyze: $ARGUMENTS

```
Task(
  subagent_type="crew:advisor",
  prompt="Analyze: $ARGUMENTS"
)
```
