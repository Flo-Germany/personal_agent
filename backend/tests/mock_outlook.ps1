# Mock Outlook window for the Phase 17 end-to-end GUI automation test.
# It is a real WinForms app, so Windows UI Automation sees ordinary Button and
# Edit controls. It never sends email; on Send it writes the field values to
# $env:MOCK_OUTLOOK_RESULT and closes.
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = "Outlook - Mock"
$form.ClientSize = New-Object System.Drawing.Size(920, 620)
$form.StartPosition = "CenterScreen"
$form.TopMost = $true
$form.BackColor = [System.Drawing.Color]::FromArgb(24, 24, 28)
$form.ForeColor = [System.Drawing.Color]::White

$title = New-Object System.Windows.Forms.Label
$title.Text = "Outlook (Mock) - Inbox"
$title.Font = New-Object System.Drawing.Font("Segoe UI", 15, [System.Drawing.FontStyle]::Bold)
$title.AutoSize = $true
$title.Location = New-Object System.Drawing.Point(28, 22)
$form.Controls.Add($title)

$newButton = New-Object System.Windows.Forms.Button
$newButton.Text = "New Email"
$newButton.Name = "NewEmailButton"
$newButton.AccessibleName = "New Email"
$newButton.Location = New-Object System.Drawing.Point(28, 70)
$newButton.Size = New-Object System.Drawing.Size(150, 42)
$form.Controls.Add($newButton)

$inbox = New-Object System.Windows.Forms.ListBox
$inbox.Name = "InboxList"
$inbox.AccessibleName = "Inbox"
$inbox.Location = New-Object System.Drawing.Point(28, 130)
$inbox.Size = New-Object System.Drawing.Size(860, 440)
$inbox.Items.Add("Welcome to the mock inbox") | Out-Null
$inbox.Items.Add("Phase 17 GUI automation test") | Out-Null
$form.Controls.Add($inbox)

$compose = New-Object System.Windows.Forms.Panel
$compose.Name = "ComposePanel"
$compose.AccessibleName = "Compose"
$compose.Location = New-Object System.Drawing.Point(28, 130)
$compose.Size = New-Object System.Drawing.Size(860, 440)
$compose.BackColor = [System.Drawing.Color]::FromArgb(34, 34, 40)
$compose.Visible = $false
$form.Controls.Add($compose)

$toLabel = New-Object System.Windows.Forms.Label
$toLabel.Text = "To:"
$toLabel.AutoSize = $true
$toLabel.Location = New-Object System.Drawing.Point(20, 24)
$compose.Controls.Add($toLabel)

$toBox = New-Object System.Windows.Forms.TextBox
$toBox.Name = "ToField"
$toBox.AccessibleName = "To"
$toBox.Location = New-Object System.Drawing.Point(110, 20)
$toBox.Size = New-Object System.Drawing.Size(700, 28)
$compose.Controls.Add($toBox)

$subjectLabel = New-Object System.Windows.Forms.Label
$subjectLabel.Text = "Subject:"
$subjectLabel.AutoSize = $true
$subjectLabel.Location = New-Object System.Drawing.Point(20, 68)
$compose.Controls.Add($subjectLabel)

$subjectBox = New-Object System.Windows.Forms.TextBox
$subjectBox.Name = "SubjectField"
$subjectBox.AccessibleName = "Subject"
$subjectBox.Location = New-Object System.Drawing.Point(110, 64)
$subjectBox.Size = New-Object System.Drawing.Size(700, 28)
$compose.Controls.Add($subjectBox)

$bodyBox = New-Object System.Windows.Forms.TextBox
$bodyBox.Name = "BodyField"
$bodyBox.AccessibleName = "Body"
$bodyBox.Multiline = $true
$bodyBox.ScrollBars = "Vertical"
$bodyBox.Location = New-Object System.Drawing.Point(20, 110)
$bodyBox.Size = New-Object System.Drawing.Size(790, 250)
$compose.Controls.Add($bodyBox)

$sendButton = New-Object System.Windows.Forms.Button
$sendButton.Text = "Send"
$sendButton.Name = "SendButton"
$sendButton.AccessibleName = "Send"
$sendButton.Location = New-Object System.Drawing.Point(700, 376)
$sendButton.Size = New-Object System.Drawing.Size(110, 42)
$compose.Controls.Add($sendButton)

$newButton.Add_Click({
    $compose.Visible = $true
    $compose.BringToFront()
    $toBox.Focus()
})

$sendButton.Add_Click({
    $resultPath = [Environment]::GetEnvironmentVariable("MOCK_OUTLOOK_RESULT")
    if ($resultPath) {
        $payload = [ordered]@{
            to = $toBox.Text
            subject = $subjectBox.Text
            body = $bodyBox.Text
            sent_at = (Get-Date).ToString("o")
        }
        $payload | ConvertTo-Json -Compress | Set-Content -LiteralPath $resultPath -Encoding UTF8
    }
    $form.Close()
})

[System.Windows.Forms.Application]::Run($form)
