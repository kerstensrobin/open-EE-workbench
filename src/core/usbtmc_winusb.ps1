# usbtmc_winusb.ps1 - bind Windows' inbox WinUSB driver to USBTMC instruments.
#
# Windows ships no USBTMC class driver, so without a vendor VISA, USB
# instruments sit driverless (Device Manager code 28) and pyvisa-py/libusb
# cannot open them. This installs a tiny INF-only driver package that matches
# the USBTMC class itself (USB\Class_FE&SubClass_03), so every instrument from
# every vendor - including ones plugged in later - binds to WinUSB.
#
# Windows refuses unsigned driver packages, so the catalog is signed with a
# single-use self-signed certificate whose private key is destroyed straight
# after signing: the trusted certificate can never sign anything else.
#
# A vendor driver (Keysight IO Libraries, NI-VISA, ...) is Microsoft-signed and
# outranks this package, so it never takes devices away from a vendor VISA.
#
# Run elevated. Driven by core/winusb.py, which prepends $Action and $WorkDir.

$ErrorActionPreference = 'Stop'
$InfName = 'open_eew_usbtmc.inf'
$Subject = 'CN=open-EE-workbench USBTMC driver (local single-use)'
$LogFile = Join-Path $WorkDir 'driver.log'

function Log([string]$msg) { Add-Content -Path $LogFile -Value $msg -Encoding UTF8 }

function Get-OurPublishedInfs {
    # pnputil /enum-drivers lists "Published Name" then "Original Name" per package.
    $published = $null
    foreach ($line in (& pnputil.exe /enum-drivers)) {
        if ($line -match '^\s*Published Name:\s*(\S+)') { $published = $Matches[1] }
        elseif ($line -match '^\s*Original Name:\s*(\S+)' -and $Matches[1] -ieq $InfName) { $published }
    }
}

function Remove-OurCertificates {
    foreach ($store in 'Root', 'TrustedPublisher') {
        Get-ChildItem "Cert:\LocalMachine\$store" |
            Where-Object { $_.Subject -like '*open-EE-workbench USBTMC driver*' } |
            Remove-Item
    }
}

function Install-Driver {
    if (Get-OurPublishedInfs) {
        Log 'Driver package already installed - rescanning devices.'
        & pnputil.exe /scan-devices | Out-Null
        return
    }

    $pkg = Join-Path $WorkDir 'pkg'
    New-Item -ItemType Directory -Force $pkg | Out-Null
    $date = Get-Date -Format 'MM/dd/yyyy'
    $inf = @"
[Version]
Signature   = "`$Windows NT`$"
Class       = USBDevice
ClassGUID   = {88BAE032-5A81-49f0-BC3D-A4FF138216D6}
Provider    = %Provider%
CatalogFile = open_eew_usbtmc.cat
DriverVer   = $date,1.0.0.0
PnpLockdown = 1

[Manufacturer]
%Provider% = Devices,NTamd64,NTarm64,NTx86

[Devices.NTamd64]
%DeviceDesc% = USBTMC_Install, USB\Class_FE&SubClass_03&Prot_01
%DeviceDesc% = USBTMC_Install, USB\Class_FE&SubClass_03

[Devices.NTarm64]
%DeviceDesc% = USBTMC_Install, USB\Class_FE&SubClass_03&Prot_01
%DeviceDesc% = USBTMC_Install, USB\Class_FE&SubClass_03

[Devices.NTx86]
%DeviceDesc% = USBTMC_Install, USB\Class_FE&SubClass_03&Prot_01
%DeviceDesc% = USBTMC_Install, USB\Class_FE&SubClass_03

[USBTMC_Install]
Include = winusb.inf
Needs   = WINUSB.NT

[USBTMC_Install.Services]
Include = winusb.inf
Needs   = WINUSB.NT.Services

[USBTMC_Install.HW]
AddReg = USBTMC_AddReg

; libusb's WinUSB backend opens devices through this interface GUID.
[USBTMC_AddReg]
HKR,,DeviceInterfaceGUIDs,0x10000,"{6E3F2A5C-8B1D-4C7E-9A42-0EE0B0A7C551}"

[Strings]
Provider   = "open-EE-workbench"
DeviceDesc = "USB Test & Measurement Device (WinUSB)"
"@
    $infPath = Join-Path $pkg $InfName
    Set-Content -Path $infPath -Value $inf -Encoding ASCII
    $cat = Join-Path $pkg 'open_eew_usbtmc.cat'
    New-FileCatalog -Path $infPath -CatalogFilePath $cat -CatalogVersion 2 | Out-Null

    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $Subject `
        -CertStoreLocation Cert:\LocalMachine\My -KeyExportPolicy NonExportable `
        -NotAfter (Get-Date).AddYears(30)
    try {
        $cer = Join-Path $WorkDir 'signer.cer'
        Export-Certificate -Cert $cert -FilePath $cer | Out-Null
        foreach ($store in 'Root', 'TrustedPublisher') {
            Import-Certificate -FilePath $cer -CertStoreLocation "Cert:\LocalMachine\$store" | Out-Null
        }
        $sig = Set-AuthenticodeSignature -FilePath $cat -Certificate $cert -HashAlgorithm SHA256
        if ($sig.Status -ne 'Valid') { throw "Catalog signing failed: $($sig.StatusMessage)" }
    } finally {
        Remove-Item -Path "Cert:\LocalMachine\My\$($cert.Thumbprint)" -DeleteKey
    }

    & pnputil.exe /add-driver $infPath /install | ForEach-Object { Log $_ }
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 3010) {
        throw "pnputil failed with exit code $LASTEXITCODE"
    }
}

function Uninstall-Driver {
    $infs = @(Get-OurPublishedInfs)
    foreach ($oem in $infs) {
        & pnputil.exe /delete-driver $oem /uninstall /force | ForEach-Object { Log $_ }
    }
    Remove-OurCertificates
    if (-not $infs) { Log 'Driver package was not installed.' }
    # Devices fall back to "no driver" (or to a vendor driver, if one exists).
    & pnputil.exe /scan-devices | Out-Null
}

$code = 0
try {
    if ($Action -eq 'uninstall') { Uninstall-Driver } else { Install-Driver }
} catch {
    Log "ERROR: $_"
    $code = 1
}
exit $code
