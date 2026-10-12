' Sudo-Service hidden launcher — starts the agent with NO console window.
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run """C:\Users\BL9\Documents\pc-remote\agent\dist\Sudo-Service.exe""", 0, False
