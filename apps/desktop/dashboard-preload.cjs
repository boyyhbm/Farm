'use strict';
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('farmDesktop', { openSystem: () => ipcRenderer.invoke('farm:open-system'), openSteam: () => ipcRenderer.invoke('farm:open-steam') });
