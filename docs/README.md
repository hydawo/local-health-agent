# docs/

`demo.gif` is generated, not hand-recorded:

```bash
brew install vhs   # or: go install github.com/charmbracelet/vhs@latest
vhs demo/demo.tape
```

The tape is the source of truth (`demo/demo.tape`); the GIF is its output.
It is not committed, because a binary that no diff can review is worse than
a 30-line script anyone can read and re-run.
