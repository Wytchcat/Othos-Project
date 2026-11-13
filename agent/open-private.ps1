# Open-Private <url>: opens a URL in a private (incognito) window of the default
# browser. Used by start-othos.ps1 and othos-tray.ps1 (dot-sourced).
# Falls back to a normal window if the browser isn't one it knows.
function Open-Private([string]$Url) {
    $exe = $null
    try {
        $prog = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice' -ErrorAction Stop).ProgId
        $cmd = (Get-ItemProperty "Registry::HKEY_CLASSES_ROOT\$prog\shell\open\command" -ErrorAction Stop).'(default)'
        if ($cmd -match '^\s*"([^"]+)"' -or $cmd -match '^\s*(\S+)') { $exe = $Matches[1] }
    } catch { }
    $flag = $null
    if ($exe) {
        switch -Regex ([IO.Path]::GetFileNameWithoutExtension($exe).ToLower()) {
            '^(opera|launcher)$'          { $flag = '--private' }      # Opera, Opera GX
            '^(chrome|brave|vivaldi)$'    { $flag = '--incognito' }
            '^msedge$'                    { $flag = '--inprivate' }
            '^firefox$'                   { $flag = '-private-window' }
        }
    }
    if ($exe -and $flag -and (Test-Path $exe)) { Start-Process -FilePath $exe -ArgumentList $flag, $Url }
    else { Start-Process $Url }
}
