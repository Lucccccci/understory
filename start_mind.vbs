' Shu - Chat Window launcher
'
' Runs launch.py with pythonw.exe so no console window appears.
' launch.py checks whether the app is already running: if so it just
' reopens the browser instead of starting a second instance.
'
' NOTE: kept ASCII-only on purpose. Windows Script Host reads .vbs as ANSI,
' so non-ASCII text here would be mangled. All Chinese text lives in the
' Python side, which handles UTF-8 properly.

Option Explicit

Dim fso, shell, baseDir, pythonw, script

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = "C:\Users\user\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\pythonw.exe"
script  = baseDir & "\launch.py"

If Not fso.FileExists(pythonw) Then
    MsgBox "pythonw.exe not found:" & vbCrLf & pythonw & vbCrLf & vbCrLf & _
           "Please check the .dsh runtime path.", 16, "Shu"
    WScript.Quit 1
End If

If Not fso.FileExists(script) Then
    MsgBox "launch.py not found:" & vbCrLf & script, 16, "Shu"
    WScript.Quit 1
End If

shell.CurrentDirectory = baseDir

' 0 = hidden window, False = do not wait for it to finish
shell.Run """" & pythonw & """ """ & script & """", 0, False
