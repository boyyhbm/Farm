; Native NSIS cross-build template. The installer contains real Electron/Windows runtimes.
; Build only with explicit APP_DIR, SETUP_OUT, PRODUCT_NAME, APP_ID and APP_EXE defines.
; It uses WriteUninstaller at install time, so compiling on Linux requires no Wine.
Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "WinVer.nsh"
!include "x64.nsh"

!ifndef APP_DIR
  !error "APP_DIR is required"
!endif
!ifndef SETUP_OUT
  !error "SETUP_OUT is required"
!endif
!ifndef PRODUCT_NAME
  !error "PRODUCT_NAME is required"
!endif
!ifndef APP_ID
  !error "APP_ID is required"
!endif
!ifndef APP_EXE
  !error "APP_EXE is required"
!endif

Name "${PRODUCT_NAME}"
OutFile "${SETUP_OUT}"
InstallDir "$LOCALAPPDATA\Programs\${APP_ID}"
InstallDirRegKey HKCU "Software\${APP_ID}" "InstallDir"
RequestExecutionLevel user
; Per-file deflate bounds compiler scratch space and shortens install startup.
SetCompressor zlib
ShowInstDetails show
ShowUninstDetails show

!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "Launch ${PRODUCT_NAME}"
!define MUI_LANGDLL_REGISTRY_ROOT HKCU
!define MUI_LANGDLL_REGISTRY_KEY "Software\${APP_ID}"
!define MUI_LANGDLL_REGISTRY_VALUENAME "InstallerLanguage"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Russian"
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_OK|MB_ICONSTOP "Windows 10/11 or a compatible Windows Server release is required."
    Abort
  ${EndIf}
  ${IfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "64-bit Windows is required."
    Abort
  ${EndIf}
  !insertmacro MUI_LANGDLL_DISPLAY
FunctionEnd

Section "${PRODUCT_NAME}" MainSection
  SetShellVarContext current
  IfFileExists "$INSTDIR\${APP_EXE}" 0 install_files
  ExecWait '$\"$INSTDIR\${APP_EXE}$\" --shutdown'
  Sleep 20000
  install_files:
  SetOutPath "$INSTDIR"
  File /r "${APP_FILES}"
  WriteUninstaller "$INSTDIR\Uninstall ${PRODUCT_NAME}.exe"
  WriteRegStr HKCU "Software\${APP_ID}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" "DisplayName" "${PRODUCT_NAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" "DisplayVersion" "0.3.0"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" "Publisher" "CS2 Farm Manager contributors"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" "UninstallString" '$\"$INSTDIR\Uninstall ${PRODUCT_NAME}.exe$\"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" "QuietUninstallString" '$\"$INSTDIR\Uninstall ${PRODUCT_NAME}.exe$\" /S'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" "NoRepair" 1
  CreateShortcut "$DESKTOP\${PRODUCT_NAME}.lnk" "$INSTDIR\${APP_EXE}"
  CreateShortcut "$SMPROGRAMS\${PRODUCT_NAME}.lnk" "$INSTDIR\${APP_EXE}"
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  ExecWait '$\"$INSTDIR\${APP_EXE}$\" --shutdown'
  Sleep 20000
  ; Optional protected SCM host must be removed before deleting its GUI controller.
  ; UAC cancellation leaves the installation intact.
  ExecWait '$\"$INSTDIR\${APP_EXE}$\" --remove-service' $0
  ${If} $0 != 0
    Abort "Windows service removal was not completed."
  ${EndIf}
  Delete "$DESKTOP\${PRODUCT_NAME}.lnk"
  Delete "$SMPROGRAMS\${PRODUCT_NAME}.lnk"
  ; Remove only this product's login item; do not affect the other installation.
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${PRODUCT_NAME}"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}"
  DeleteRegKey HKCU "Software\${APP_ID}"
  RMDir /r "$INSTDIR"
  IfSilent keep_user_data
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "Also delete local settings and database for ${PRODUCT_NAME}? This cannot be undone." IDNO keep_user_data
  RMDir /r "$APPDATA\${PRODUCT_NAME}"
  keep_user_data:
SectionEnd
