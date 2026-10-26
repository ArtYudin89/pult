# Pult deploy from Windows: copy the code to the server over ssh and run deploy/install.sh there.
#   powershell -NoProfile -File deploy\deploy.ps1
# Target: .local\deploy.json {"ssh": "root@server", "key": "~/.ssh/id_ed25519"} ("key" is optional).
# Server settings: .local\pult.conf -> /etc/pult/pult.conf (sample: deploy\pult.conf.example).
# ASCII only on purpose: PowerShell 5.1 reads BOM-less UTF-8 scripts as cp1251.
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}
$root = Split-Path -Parent $PSScriptRoot
$cfg = Get-Content (Join-Path $root ".local\deploy.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$target = $cfg.ssh
$sshArgs = @()
if ($cfg.key) { $sshArgs = @("-i", ($cfg.key -replace "^~", $env:USERPROFILE)) }
$stage = "/tmp/pult_deploy"

ssh @sshArgs $target "rm -rf $stage; mkdir -p $stage/server/static/fonts $stage/deploy"
if (-not $?) { exit 1 }
scp -q @sshArgs "$root\server\pult_server.py" "${target}:$stage/server/"
$static = Get-ChildItem "$root\server\static" -File | ForEach-Object { $_.FullName }
scp -q @sshArgs $static "${target}:$stage/server/static/"
$fonts = Get-ChildItem "$root\server\static\fonts" -File | ForEach-Object { $_.FullName }
scp -q @sshArgs $fonts "${target}:$stage/server/static/fonts/"
$deploy = Get-ChildItem "$root\deploy" -File | Where-Object { $_.Extension -ne ".ps1" } | ForEach-Object { $_.FullName }
scp -q @sshArgs $deploy "${target}:$stage/deploy/"
$pub = Join-Path $root ".local\keys_public.json"
if (Test-Path $pub) { scp -q @sshArgs $pub "${target}:$stage/keys_public.json" }
$conf = Join-Path $root ".local\pult.conf"
if (Test-Path $conf) { scp -q @sshArgs $conf "${target}:$stage/pult.conf" }
ssh @sshArgs $target "if [ -f $stage/pult.conf ]; then install -d -m 0755 /etc/pult && install -m 0644 $stage/pult.conf /etc/pult/pult.conf; fi; sh $stage/deploy/install.sh; rc=`$?; rm -rf $stage; exit `$rc"
