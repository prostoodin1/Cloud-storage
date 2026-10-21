import { Directory, File, Paths } from 'expo-file-system';

import { ApiError, CloudApi } from './api';
import type { TransferRecord } from './types';

const CHUNK_SIZE = 4 * 1024 * 1024;

export function negotiatedChunkSize(advertised?: number): number {
  if (!Number.isSafeInteger(advertised) || (advertised ?? 0) < 256 * 1024) return CHUNK_SIZE;
  // Keep native Blob allocations modest on phones while reducing WAN round trips.
  return Math.min(advertised!, 16 * 1024 * 1024);
}

export function isRetryableTransferError(error: unknown): boolean {
  return error instanceof TypeError ||
    (error instanceof ApiError && [408, 429, 502, 503, 504].includes(error.status));
}

export type TransferUpdate = (record: TransferRecord) => void;

export async function runUploadTransfer(
  api: CloudApi,
  initial: TransferRecord,
  onUpdate: TransferUpdate,
  maxChunks = Number.POSITIVE_INFINITY,
): Promise<TransferRecord> {
  let record: TransferRecord = { ...initial, status: 'running', error: undefined };
  onUpdate(record);
  const source = new File(record.localUri);
  if (!source.exists) {
    throw new Error('Локальный файл очереди больше не существует.');
  }
  const totalBytes = record.totalBytes || source.size;
  let remoteUploadId = record.remoteUploadId ?? '';
  let offset = record.completedBytes;
  let chunkSize = CHUNK_SIZE;
  if (remoteUploadId) {
    try {
      const status = await api.uploadStatus(remoteUploadId);
      chunkSize = negotiatedChunkSize(status.max_chunk_bytes);
      offset = Number(status.received_bytes ?? offset);
      if (status.status === 'completed') {
        record = { ...record, totalBytes, completedBytes: totalBytes, status: 'completed' };
        onUpdate(record);
        return record;
      }
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 404) throw error;
      remoteUploadId = '';
      offset = 0;
    }
  }
  if (!remoteUploadId) {
    const created = await api.createUpload(
      record.spaceId,
      record.logicalPath,
      totalBytes,
      source.type || 'application/octet-stream',
    );
    remoteUploadId = created.id;
    chunkSize = negotiatedChunkSize(created.max_chunk_bytes);
    offset = Number(created.received_bytes ?? 0);
    record = { ...record, remoteUploadId, totalBytes, completedBytes: offset };
    onUpdate(record);
  }
  let chunks = 0;
  if (!Number.isSafeInteger(offset) || offset < 0 || offset > totalBytes) {
    throw new Error('Сервер сообщил неверное смещение загрузки.');
  }
  while (offset < totalBytes && chunks < maxChunks) {
    const end = Math.min(totalBytes, offset + chunkSize);
    const chunk = source.slice(offset, end);
    const acknowledged = await api.uploadChunk(remoteUploadId, offset, chunk);
    if (acknowledged !== end) throw new Error('Сервер подтвердил неверное смещение загрузки.');
    offset = acknowledged;
    chunks += 1;
    record = { ...record, completedBytes: offset };
    onUpdate(record);
  }
  if (offset < totalBytes) {
    record = { ...record, status: 'queued' };
    onUpdate(record);
    return record;
  }
  await api.completeUpload(remoteUploadId);
  record = { ...record, completedBytes: totalBytes, status: 'completed', error: undefined };
  onUpdate(record);
  source.delete();
  return record;
}

export async function runDownloadTransfer(
  api: CloudApi,
  initial: TransferRecord,
  onUpdate: TransferUpdate,
): Promise<TransferRecord> {
  let record: TransferRecord = { ...initial, status: 'running', error: undefined };
  onUpdate(record);
  const destination = new File(record.localUri);
  const partial = new File(record.localUri + '.' + record.id + '.cloud-part');
  if (partial.exists) partial.delete();
  let lastProgressAt = 0;
  let lastProgressBytes = 0;
  const task = File.createDownloadTask(
    api.fileUrl(record.spaceId, record.logicalPath),
    partial,
    {
      headers: api.fileHeaders(),
      sessionType: 'background',
      onProgress: ({ bytesWritten, totalBytes }) => {
        const now = Date.now();
        if (now - lastProgressAt < 250 && bytesWritten - lastProgressBytes < 16 * 1024 * 1024) return;
        lastProgressAt = now;
        lastProgressBytes = bytesWritten;
        record = {
          ...record,
          completedBytes: bytesWritten,
          totalBytes: totalBytes > 0 ? totalBytes : record.totalBytes,
        };
        onUpdate(record);
      },
    },
  );
  const downloaded = await task.downloadAsync();
  if (!downloaded?.exists) throw new Error('Скачанный файл не найден.');
  if (initial.totalBytes > 0 && downloaded.size !== initial.totalBytes) {
    throw new Error('Файл скачан не полностью. Существующая копия сохранена.');
  }
  await downloaded.move(destination, { overwrite: true });
  record = {
    ...record,
    completedBytes: downloaded.size,
    totalBytes: downloaded.size,
    status: 'completed',
  };
  onUpdate(record);
  return record;
}

export function uploadQueueDirectory(): Directory {
  const directory = new Directory(Paths.document, 'Cloud Storage', 'Upload Queue');
  directory.create({ idempotent: true, intermediates: true });
  return directory;
}

export function offlineDirectory(): Directory {
  const directory = new Directory(Paths.document, 'Cloud Storage', 'Offline');
  directory.create({ idempotent: true, intermediates: true });
  return directory;
}

export function safeFileName(value: string): string {
  return value.replace(/[\\/:*?"<>|]/g, '_').slice(0, 180) || 'file.bin';
}
