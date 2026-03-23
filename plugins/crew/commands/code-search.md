---
description: Search codebase for files, patterns, and implementations
---

Spawn a **reader agent** to search for: $ARGUMENTS

```
Task(
  subagent_type="crew:reader",
  prompt="Search the codebase for: $ARGUMENTS\n\nBe thorough - try multiple search strategies, naming conventions, and common locations. Report ALL relevant matches with absolute paths.",
  model="haiku"
)
```

The reader agent will:
- Use parallel search strategies (glob, grep, symbol search)
- Try alternative naming conventions (camelCase, snake_case, kebab-case)
- Search common locations (src/, lib/, services/, utils/)
- Return structured results with absolute paths and context
