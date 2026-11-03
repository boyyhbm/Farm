; Existing user databases survive an upgrade/uninstall unless the user explicitly deletes them.
; No firewall rule, Defender exception, certificate-store change or SmartScreen bypass is installed.
!macro customUnInstall
  IfSilent farm_keep_data
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "Удалить также настройки и локальные данные ${PRODUCT_NAME}? Эта операция необратима. / Also delete local settings and data? This cannot be undone." IDNO farm_keep_data
  RMDir /r "$APPDATA\${PRODUCT_NAME}"
  farm_keep_data:
!macroend
