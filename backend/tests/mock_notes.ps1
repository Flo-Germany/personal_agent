# Secondary WinForms mock for Phase 19: text field, popup, and resizing.
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = "Notes - Mock"
$form.ClientSize = New-Object System.Drawing.Size(760, 520)
$form.StartPosition = "CenterScreen"
$form.TopMost = $true
$form.BackColor = [System.Drawing.Color]::FromArgb(28, 30, 38)
$form.ForeColor = [System.Drawing.Color]::White

$label = New-Object System.Windows.Forms.Label
$label.Text = "Quick note"
$label.AutoSize = $true
$label.Location = New-Object System.Drawing.Point(28, 24)
$form.Controls.Add($label)

$note = New-Object System.Windows.Forms.TextBox
$note.Name = "NoteField"
$note.AccessibleName = "Note"
$note.Multiline = $true
$note.ScrollBars = "Vertical"
$note.Location = New-Object System.Drawing.Point(28, 60)
$note.Size = New-Object System.Drawing.Size(700, 330)
$form.Controls.Add($note)

$save = New-Object System.Windows.Forms.Button
$save.Text = "Save"
$save.Name = "SaveNoteButton"
$save.AccessibleName = "Save"
$save.Location = New-Object System.Drawing.Point(500, 410)
$save.Size = New-Object System.Drawing.Size(110, 40)
$form.Controls.Add($save)

$popup = New-Object System.Windows.Forms.Button
$popup.Text = "Show Popup"
$popup.Name = "ShowPopupButton"
$popup.AccessibleName = "Show Popup"
$popup.Location = New-Object System.Drawing.Point(28, 410)
$popup.Size = New-Object System.Drawing.Size(140, 40)
$form.Controls.Add($popup)

$resize = New-Object System.Windows.Forms.Button
$resize.Text = "Resize Window"
$resize.Name = "ResizeButton"
$resize.AccessibleName = "Resize Window"
$resize.Location = New-Object System.Drawing.Point(180, 410)
$resize.Size = New-Object System.Drawing.Size(140, 40)
$form.Controls.Add($resize)

$save.Add_Click({
    $path = [Environment]::GetEnvironmentVariable("MOCK_NOTES_RESULT")
    if ($path) {
        [System.IO.File]::WriteAllText($path, $note.Text, [System.Text.Encoding]::UTF8)
    }
    $form.Close()
})

$popup.Add_Click({
    [System.Windows.Forms.MessageBox]::Show(
        "Unexpected update available. Review before continuing.",
        "Compa Popup",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Warning
    ) | Out-Null
    $path = [Environment]::GetEnvironmentVariable("MOCK_NOTES_POPUP_RESULT")
    if ($path) {
        [System.IO.File]::WriteAllText($path, "popup-ok", [System.Text.Encoding]::UTF8)
    }
})

$resize.Add_Click({
    $form.Size = New-Object System.Drawing.Size(560, 420)
})

[System.Windows.Forms.Application]::Run($form)
