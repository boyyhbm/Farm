'use strict';
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('farmSetup', {
  invoke: (action, payload) => ipcRenderer.invoke('farm:management', action, payload),
  onStatus: callback => {
    const listener = (_event, value) => callback(value);
    ipcRenderer.on('farm:status', listener);
    return () => ipcRenderer.removeListener('farm:status', listener);
  },
  onEnrollment: callback => {
    const listener = (_event, value) => callback(value);
    ipcRenderer.on('farm:enrollment', listener);
    return () => ipcRenderer.removeListener('farm:enrollment', listener);
  },
});
