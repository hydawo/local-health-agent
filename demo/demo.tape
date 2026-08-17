# Generates docs/demo.gif with VHS — https://github.com/charmbracelet/vhs
#
#   brew install vhs
#   vhs demo/demo.tape
#
# Checked in rather than hand-recorded so the demo is reproducible and
# reviewable: anyone can regenerate it, and a diff shows what changed.
#
# It shows LOCAL mode with no flags — that is the default and the product.
# The `ask` steps need Ollama with qwen3.6:27b pulled; without it, swap the
# Type line below for `./demo/demo.sh --fast`.

Output docs/demo.gif

Set Shell bash
Set FontSize 15
Set Width 1280
Set Height 820
Set Padding 22
Set Theme "Catppuccin Mocha"
Set TypingSpeed 28ms
Set PlaybackSpeed 1.0

# Slower pauses read better in a GIF than the script's default.
Env DEMO_PAUSE "2"

Type "./demo/demo.sh"
Sleep 400ms
Enter

# The local model takes 30-60s per question; the ingest and query steps are
# fast. This budget covers the whole run including two `ask` calls.
Sleep 220s

Screenshot docs/demo-last-frame.png
