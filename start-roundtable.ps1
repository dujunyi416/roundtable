param([switch]$NoBrowser)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$Url = "http://127.0.0.1:8642"

if (-not (Test-Path -LiteralPath $Python)) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        "Roundtable virtual environment was not found:`n$Python",
        "Roundtable",
        "OK",
        "Error"
    ) | Out-Null
    exit 1
}

function Test-Roundtable {
    try {
        $response = Invoke-WebRequest -UseBasicParsing $Url -TimeoutSec 1
        return $response.StatusCode -eq 200 -and $response.Content -like "*<title>Roundtable</title>*"
    } catch {
        return $false
    }
}

if (-not (Test-Roundtable)) {
    $portBusy = Get-NetTCPConnection -LocalPort 8642 -State Listen -ErrorAction SilentlyContinue
    if ($portBusy) {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show(
            "Port 8642 is already being used by another program.",
            "Roundtable",
            "OK",
            "Error"
        ) | Out-Null
        exit 1
    }

    $arguments = "-u -m roundtable ui --cwd `"$ProjectDir`" --port 8642"
    Start-Process -FilePath $Python -ArgumentList $arguments `
        -WorkingDirectory $ProjectDir -WindowStyle Hidden

    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if (Test-Roundtable) { break }
        Start-Sleep -Milliseconds 250
    }
}

if (-not (Test-Roundtable)) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        "Roundtable did not start successfully.",
        "Roundtable",
        "OK",
        "Error"
    ) | Out-Null
    exit 1
}

if (-not $NoBrowser) {
    Start-Process $Url
}
