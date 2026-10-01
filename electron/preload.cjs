const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('agw', {
  api: (method, route, body) => ipcRenderer.invoke('api', method, route, body),
  checkUpdates: () => ipcRenderer.invoke('check-updates'),
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
  appInfo: () => ipcRenderer.invoke('app-info'),
  on: (channel, cb) => {
    if (!['update-info', 'daemon-status'].includes(channel)) return;
    ipcRenderer.on(channel, (_e, data) => cb(data));
  },
});
