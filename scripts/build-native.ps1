# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors
param([Parameter(Mandatory=$true)][string]$Zig)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$componentNative = Join-Path $projectRoot 'custom_components/aqara_p3/native'
$nativeSource = Join-Path $componentNative 'p3lan.c'
$nativeBinary = Join-Path $componentNative 'p3lan-helper'
& $Zig cc -target mipsel-linux.3.10-musleabi -mcpu=mips32r2 -msoft-float -Os -static -s $nativeSource -o $nativeBinary
if ($LASTEXITCODE -ne 0) { throw 'Native build failed' }
$nativeHash = (Get-FileHash -LiteralPath $nativeBinary -Algorithm SHA256).Hash.ToLowerInvariant()
$nativeInfo = @(
    '# SPDX-License-Identifier: GPL-3.0-only'
    '# Copyright (c) 2026 Aqara P3 contributors'
    ''
    ('SHA256 = "' + $nativeHash + '"')
)
Set-Content -LiteralPath (Join-Path $projectRoot 'custom_components/aqara_p3/protocol/native_info.py') -Value $nativeInfo -Encoding utf8
Write-Output "Built $nativeBinary ($nativeHash)"
