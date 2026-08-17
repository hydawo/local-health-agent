---
title: "Frontmatter the simple parser only partly understands"
created: 03/14/2026
tags: exercise, recovery
nested:
  structure: not supported
  another: value
weird line without a colon
count: 3
enabled: true
---

# Frontmatter edge cases

This note exists to pin down what happens when frontmatter goes beyond the
subset the parser handles: a US-format date, a comma-separated tag string
rather than a list, a nested map, and a line that is not a key-value pair at
all.

The expectation is that the understandable keys are read, the rest is preserved
verbatim under `_unparsed`, and the note is ingested normally either way. A note
should never be lost because its header was unusual.

## Code block check

The fenced block below contains something that looks like a heading. It must not
be treated as one, or the section trail for this note would be wrong.

```python
# This is a comment, not a markdown heading
def resting_heart_rate():
    return "not a real function"
```

That block should stay inside this section.
