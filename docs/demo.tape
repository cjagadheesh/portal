# Record the README demo with https://github.com/charmbracelet/vhs once you have a real
# deployment:  vhs docs/demo.tape   (writes docs/demo.gif)
# Uses the real tool against real AWS, so run it in a throwaway account and state.
Output docs/demo.gif
Set FontSize 16
Set Width 1000
Set Height 600
Set Theme "Dracula"

Type "portal up --ttl 1h" Enter
Wait /connect with/
Sleep 1s
Type "portal ssh" Enter
Wait /dev@/
Type "docker run --rm hello-world | head -3" Enter
Sleep 3s
Type "exit" Enter
Type "portal cost" Enter
Sleep 3s
Type "portal down --yes" Enter
Sleep 3s
