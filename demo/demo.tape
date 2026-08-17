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

# Most of this recording is the local model thinking, so play it back at 2x and
# drop the framerate: a terminal is mostly static frames, and 12fps costs
# nothing visually while roughly halving the file again.
Set PlaybackSpeed 2.0
Set Framerate 12

# Slower pauses read better in a GIF than the script's default.
Env DEMO_PAUSE "2"

Type "./demo/demo.sh"
Sleep 400ms
Enter

# Measured, not guessed: the full demo runs in ~106s on an M-series Mac with
# DEMO_PAUSE=2, almost all of it the two `ask` calls. The headroom is for model
# variance, which is large — the same question has taken 25s and 170s across
# eval runs. Re-time this if you change what the demo does; a GIF that cuts off
# mid-answer looks like a broken tool.
Sleep 150s
