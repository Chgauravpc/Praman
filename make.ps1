<#
    Windows shim for the Makefile. Same targets, same commands.

    GNU make is not installed by default on Windows and this project was built on
    a Windows box, so the Makefile could not be exercised locally. Rather than
    ship a Makefile nobody had run, the commands live in both files and this shim
    is what the build actually used. The Makefile remains canonical for reviewers
    on Linux, macOS or WSL.

    Usage:  .\make.ps1 demo | demo-full | demo-live | test | verify | golden | clean
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('help', 'demo', 'demo-full', 'demo-live', 'test', 'verify', 'golden', 'clean', 'install',
                 'execute', 'execute-full', 'execute-live')]
    [string]$Target = 'help'
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

function Invoke-Demo {
    param([string]$Batch = '--dev', [string]$Offline = '1')
    $env:PRAMAAN_LLM_OFFLINE = $Offline
    & python -m pramaan.cli demo $Batch
    if ($LASTEXITCODE -ne 0) { throw "demo failed with exit code $LASTEXITCODE" }
}

switch ($Target) {
    'help' {
        Write-Output 'Pramaan -- targets'
        Write-Output '  .\make.ps1 demo        200-event dev batch. No API key needed. Start here.'
        Write-Output '  .\make.ps1 demo-full   6,000-event batch (sized from the PRD power calc)'
        Write-Output '  .\make.ps1 demo-live   re-run against live APIs and refresh the cache'
        Write-Output '  .\make.ps1 test        the full test suite, including invariants I1/I2/I7'
        Write-Output '  .\make.ps1 verify      test + demo determinism check (I8)'
        Write-Output '  .\make.ps1 golden      regenerate the golden ledger. Read the diff.'
        Write-Output '  .\make.ps1 clean       remove build artefacts'
        Write-Output '  .\make.ps1 execute        Day 5: plan -> envelope -> resolve, arm C wired, shadow mode'
        Write-Output '  .\make.ps1 execute-full   the same, on the 6,000-event batch'
        Write-Output '  .\make.ps1 execute-live   also creates one real order + payment link in Razorpay TEST mode'
    }

    'install' { & python -m pip install -r requirements.txt }

    'demo'      { Invoke-Demo '--dev' '1' }
    'demo-full' { Invoke-Demo '--full' '1' }
    'demo-live' { Invoke-Demo '--dev' '0' }

    'test' {
        $env:PRAMAAN_LLM_OFFLINE = '1'
        & python -m pytest tests -q
        if ($LASTEXITCODE -ne 0) { throw "tests failed with exit code $LASTEXITCODE" }
    }

    'verify' {
        # Invariant I8: same seed plus same cache produces byte-identical output.
        $env:PRAMAAN_LLM_OFFLINE = '1'
        & python -m pytest tests -q
        if ($LASTEXITCODE -ne 0) { throw "tests failed with exit code $LASTEXITCODE" }

        $a = Join-Path $env:TEMP 'pramaan-run-a.txt'
        $b = Join-Path $env:TEMP 'pramaan-run-b.txt'
        & python -m pramaan.cli demo --dev | Out-File -FilePath $a -Encoding utf8
        & python -m pramaan.cli demo --dev | Out-File -FilePath $b -Encoding utf8
        if ((Get-FileHash $a).Hash -eq (Get-FileHash $b).Hash) {
            Write-Output 'I8 PASS: two runs are byte-identical'
        }
        else {
            Compare-Object (Get-Content $a) (Get-Content $b)
            throw 'I8 FAIL: demo output is not reproducible'
        }
    }

    'golden' {
        $env:PRAMAAN_LLM_OFFLINE = '1'
        & python -m pramaan.cli demo --dev | Out-Null
        Copy-Item 'build/ledger-dev.jsonl' 'tests/golden/ledger.jsonl' -Force
        Write-Output 'golden ledger regenerated -- read the diff before committing it'
    }

    'execute' {
        $env:PRAMAAN_LLM_OFFLINE = '1'
        & python -m pramaan.cli execute --dev
        if ($LASTEXITCODE -ne 0) { throw "execute failed with exit code $LASTEXITCODE" }
    }

    'execute-full' {
        $env:PRAMAAN_LLM_OFFLINE = '1'
        & python -m pramaan.cli execute --full
        if ($LASTEXITCODE -ne 0) { throw "execute --full failed with exit code $LASTEXITCODE" }
    }

    'execute-live' {
        $env:PRAMAAN_LLM_OFFLINE = '1'
        & python -m pramaan.cli execute --dev --live-razorpay
        if ($LASTEXITCODE -ne 0) { throw "execute --live-razorpay failed with exit code $LASTEXITCODE" }
    }

    'clean' {
        if (Test-Path 'build') { Remove-Item 'build' -Recurse -Force }
        if (Test-Path '.pytest_cache') { Remove-Item '.pytest_cache' -Recurse -Force }
        Get-ChildItem -Path . -Filter '__pycache__' -Recurse -Directory |
            Remove-Item -Recurse -Force
    }
}
