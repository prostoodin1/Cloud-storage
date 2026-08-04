import * as BackgroundTask from 'expo-background-task';
import * as TaskManager from 'expo-task-manager';

import { CloudApi } from './api';
import { queuePhotoBackupBatch } from './photoBackup';
import { loadDeviceToken, loadRemoteSession, loadState, saveState } from './storage';
import { runUploadTransfer } from './transfers';

export const TRANSFER_BACKGROUND_TASK = 'cloud-storage-transfer-retry-v1';

TaskManager.defineTask(TRANSFER_BACKGROUND_TASK, async () => {
  let state = loadState();
  const connection = state.connection;
  if (!connection) return BackgroundTask.BackgroundTaskResult.Success;
  try {
    const [deviceToken, remoteSession] = await Promise.all([
      loadDeviceToken(),
      loadRemoteSession(),
    ]);
    if (!deviceToken) return BackgroundTask.BackgroundTaskResult.Success;
    const api = new CloudApi(connection.serverUrl, deviceToken, remoteSession);
    if (state.photoBackup.enabled) {
      const result = await queuePhotoBackupBatch(state, 3, false);
      state = result.state;
      saveState(state);
    }
    const candidate = state.transfers.find(
      (item) => item.direction === 'upload' && ['queued', 'running'].includes(item.status),
    );
    if (!candidate) return BackgroundTask.BackgroundTaskResult.Success;
    await runUploadTransfer(
      api,
      candidate,
      (updated) => {
        const current = loadState();
        saveState({
          ...current,
          transfers: current.transfers.map((item) => (item.id === updated.id ? updated : item)),
        });
      },
      2,
    );
    return BackgroundTask.BackgroundTaskResult.Success;
  } catch {
    return BackgroundTask.BackgroundTaskResult.Failed;
  }
});

export async function ensureBackgroundTransfersRegistered(): Promise<void> {
  const registered = await TaskManager.isTaskRegisteredAsync(TRANSFER_BACKGROUND_TASK);
  if (!registered) {
    await BackgroundTask.registerTaskAsync(TRANSFER_BACKGROUND_TASK, { minimumInterval: 15 });
  }
}
