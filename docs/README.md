# docs/

`demo.gif` is generated, not hand-recorded:

```bash
brew install vhs   # or: go install github.com/charmbracelet/vhs@latest
vhs demo/demo.tape
```

The tape is the source of truth (`demo/demo.tape`); the GIF is its output. Both
are committed — the README needs the GIF to render on GitHub, and committing the
tape next to it keeps the binary reviewable and reproducible, which is the
property that actually mattered.

Two things the tape assumes, because it types a bare `./demo/demo.sh` into a
fresh shell:

- **`health-agent` is on `PATH`** — activate the virtualenv you installed into
  before running `vhs`, since the recorded shell inherits its environment from
  the process that launched it. (`demo.sh` falls back to
  `python -m health_agent.cli`, but only if that `python` is the right one.)
- **Ollama is running with `qwen3.6:27b` pulled**, for the two `ask` steps. To
  record without a local model, change the tape's `Type` line to
  `./demo/demo.sh --fast` and cut the `Sleep` to about 20s.

The `Sleep` budget in the tape is measured against a real run, not guessed. If
you change what the demo does, re-time it — a GIF that cuts off mid-answer is
the failure mode here, and it isn't visible until you watch the whole thing.
