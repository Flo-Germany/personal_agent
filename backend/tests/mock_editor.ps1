# Phase 21 GUI experiment target: a small editor with UIA-verifiable state.
#
# The autonomous harness launches this form, asks Compa to type into it and set
# the font size, then reads PHASE21_EDITOR_RESULT to verify the exact result.
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = "Phase21 Editor"
$form.ClientSize = New-Object System.Drawing.Size(760, 540)
$form.StartPosition = "CenterScreen"
$form.BackColor = [System.Drawing.Color]::FromArgb(28, 30, 38)
$form.ForeColor = [System.Drawing.Color]::White
$form.Font = New-Object System.Drawing.Font("Segoe UI", 11)

$title = New-Object System.Windows.Forms.Label
$title.Text = "Phase 21 Editor"
$title.AutoSize = $true
$title.Location = New-Object System.Drawing.Point(28, 22)
$title.Font = New-Object System.Drawing.Font("Segoe UI", 15, [System.Drawing.FontStyle]::Bold)
$form.Controls.Add($title)

$note = New-Object System.Windows.Forms.TextBox
$note.Name = "EditorField"
$note.AccessibleName = "Editor"
$note.Multiline = $true
$note.ScrollBars = "Vertical"
$note.Location = New-Object System.Drawing.Point(28, 62)
$note.Size = New-Object System.Drawing.Size(700, 330)
$note.Font = New-Object System.Drawing.Font("Segoe UI", 12)
$form.Controls.Add($note)

$fontLabel = New-Object System.Windows.Forms.Label
$fontLabel.Text = "Size"
$fontLabel.AutoSize = $true
$fontLabel.Location = New-Object System.Drawing.Point(28, 412)
$form.Controls.Add($fontLabel)

$fontSize = New-Object System.Windows.Forms.TextBox
$fontSize.Name = "FontSizeField"
$fontSize.AccessibleName = "Font size"
$fontSize.Text = "12"
$fontSize.Location = New-Object System.Drawing.Point(80, 408)
$fontSize.Size = New-Object System.Drawing.Size(70, 30)
$form.Controls.Add($fontSize)

$apply = New-Object System.Windows.Forms.Button
$apply.Name = "ApplyFontButton"
$apply.AccessibleName = "Apply font"
$apply.Text = "Apply font"
$apply.Location = New-Object System.Drawing.Point(170, 406)
$apply.Size = New-Object System.Drawing.Size(120, 34)
$form.Controls.Add($apply)

$status = New-Object System.Windows.Forms.Label
$status.Name = "AppliedFontLabel"
$status.AccessibleName = "Applied font size"
$status.Text = "Applied font size: 12"
$status.AutoSize = $true
$status.Location = New-Object System.Drawing.Point(310, 414)
$form.Controls.Add($status)

$save = New-Object System.Windows.Forms.Button
$save.Name = "SaveButton"
$save.AccessibleName = "Save"
$save.Text = "Save"
$save.Location = New-Object System.Drawing.Point(600, 406)
$save.Size = New-Object System.Drawing.Size(120, 34)
$form.Controls.Add($save)

$script:applied = 12

$apply.Add_Click({
    $value = 0
    if ([int]::TryParse($fontSize.Text.Trim(), [ref]$value) -and $value -ge 8 -and $value -le 72) {
        $script:applied = $value
        $note.Font = New-Object System.Drawing.Font("Segoe UI", [float]$script:applied)
        $status.Text = "Applied font size: $($script:applied)"
    } else {
        $status.Text = "Enter a size from 8 to 72"
    }
})

$save.Add_Click({
    $path = [Environment]::GetEnvironmentVariable("PHASE21_EDITOR_RESULT")
    if ($path) {
        $payload = [ordered]@{
            text = $note.Text
            font_size = $script:applied
            field_value = $fontSize.Text
            saved_at = (Get-Date).ToString("o")
        }
        $json = $payload | ConvertTo-Json -Compress
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($path, $json, $utf8)
    }
    $form.Close()
})

[System.Windows.Forms.Application]::Run($form)
