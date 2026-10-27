Option Explicit
Dim fso, shell, folder, pythonw, command
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = fso.BuildPath(folder, ".venv\Scripts\pythonw.exe")
If fso.FileExists(pythonw) Then
    command = Chr(34) & pythonw & Chr(34)
Else
    command = "pyw -3"
End If
shell.CurrentDirectory = folder
On Error Resume Next
shell.Run command & " " & Chr(34) & fso.BuildPath(folder, "desktop.py") & Chr(34), 0, False
If Err.Number <> 0 Then
    MsgBox "Python is not installed. Please use the Windows installer edition.", 48, "Course Material Assistant"
End If
