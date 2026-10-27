import * as BackgroundTask from 'expo-background-task';
import * as TaskManager from 'expo-task-manager';

import { CloudApi } from './api';
import { queuePhotoBackupBatch } from './photoBackup';
import { loadDeviceToken, loadRemoteSession, loadState, saveState } from './storage';
import { isRetryableTransferError, runUploadTransfer } from './transfers';
import { claimTransfer, TransferLeaseLost } from './transferLease';

export const TRANSFER_BACKGROUND_TASK = 'cloud-storage-transfer-retry-v1';
let backgroundTaskRunning = false;

TaskManager.defineTask(TRANSFER_BACKGROUND_TASK, async () => {
  if (backgroundTaskRunning) return BackgroundTask.BackgroundTaskResult.Success;
  backgroundTaskRunning = true;
  let state = loadState();
  let lease: ReturnType<typeof claimTransfer> = null;
  const connection = state.connection;
  if (!connection) {
    backgroundTaskRunning = false;
    return BackgroundTask.BackgroundTaskResult.Success;
  }
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
    lease = claimTransfer(connection.serverUrl, candidate.id);
    if (!lease) return BackgroundTask.BackgroundTaskResult.Success;
    const owned = lease.latest() ?? candidate;
    if (!['queued','running'].includes(owned.status)) return BackgroundTask.BackgroundTaskResult.Success;
    await runUploadTransfer(
      api,
      owned,
      (updated) => {
        lease!.assertActive();
        lease!.checkpoint(updated);
        const current = loadState();
        saveState({
          ...current,
          transfers: current.transfers.map((item) => (item.id === updated.id ? updated : item)),
        });
      },
      2,
    );
    return BackgroundTask.BackgroundTaskResult.Success;
  } catch (error) {
    if (error instanceof TransferLeaseLost) return BackgroundTask.BackgroundTaskResult.Success;
    const current = lease?.latest();
    if (current) {
      try { lease!.checkpoint({ ...current, status: isRetryableTransferError(error) ? 'queued' : 'failed', error: error instanceof Error ? error.message : 'Ошибка передачи' }); } catch { /* Lease may have expired. */ }
    }
    return BackgroundTask.BackgroundTaskResult.Failed;
  } finally {
    lease?.release();
    backgroundTaskRunning = false;
  }
});

export async function ensureBackgroundTransfersRegistered(): Promise<void> {
  const registered = await TaskManager.isTaskRegisteredAsync(TRANSFER_BACKGROUND_TASK);
  if (!registered) {
    await BackgroundTask.registerTaskAsync(TRANSFER_BACKGROUND_TASK, { minimumInterval: 15 });
  }
}
