import { Directory, File, Paths } from 'expo-file-system';
import { sha256 } from '@noble/hashes/sha2.js';

import { ApiError, CloudApi, retryAfterMs } from './api';
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

export function transferRetryDelay(attempt: number, error: unknown): number {
  const base = Math.min(60_000, 1000 * 2 ** Math.min(6, Math.max(1, attempt)));
  const hint = error instanceof ApiError ? error.retryAfterMs ?? 0 : 0;
  return Math.max(base + Math.random() * base * 0.2, Number.isFinite(hint) ? Math.min(3_600_000, hint) : 0);
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
  assertActive: () => void = () => {},
): Promise<TransferRecord> {
  let record: TransferRecord = { ...initial, status: 'running', error: undefined };
  onUpdate(record);
  const destination = new File(record.localUri);
  const partial = new File(record.localUri + '.' + record.id + '.cloud-part');
  const digest = /^[a-f0-9]{64}$/i.test(initial.expectedSha256 ?? '') ? initial.expectedSha256!.toLowerCase() : '';
  if (partial.exists && (!digest || partial.size > initial.totalBytes)) partial.delete();
  let ranged = Boolean(digest && initial.totalBytes > 0);
  if (ranged) {
    if (!partial.exists) partial.create();
    while (partial.size < initial.totalBytes) {
      assertActive();
      const offset = partial.size;
      const count = Math.min(16 * 1024 * 1024, initial.totalBytes - offset);
      const response = await api.downloadRange(record.spaceId, record.logicalPath, offset, count, digest);
      if (response.status === 200) {
        await response.body?.cancel();
        partial.delete();
        ranged = false; // A legacy server may ignore Range; verify its full download below.
        break;
      }
      if (response.status !== 206) {
        await response.body?.cancel();
        throw new ApiError(response.status, 'Сервер не принял догрузку файла.', retryAfterMs(response.headers.get('Retry-After')));
      }
      const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(response.headers.get('Content-Range') ?? '');
      if (!match || Number(match[1]) !== offset || Number(match[2]) >= offset + count || Number(match[2]) < offset || Number(match[3]) !== initial.totalBytes) {
        await response.body?.cancel();
        throw new ApiError(409, 'Сервер вернул неверный диапазон файла.');
      }
      const reader = response.body?.getReader();
      if (!reader) throw new TypeError('Сервер не передал содержимое файла.');
      const handle = partial.open();
      handle.offset = offset;
      let received = 0;
      try {
        while (true) {
          let timer: ReturnType<typeof setTimeout> | undefined;
          let item: ReadableStreamReadResult<Uint8Array>;
          try {
            item = await Promise.race([reader.read(), new Promise<never>((_, reject) => {
              timer = setTimeout(() => reject(new TypeError('Поток скачивания остановился.')), 30_000);
            })]);
          } finally { clearTimeout(timer); }
          if (item.done) break;
          assertActive();
          if (received + item.value.length > Number(match[2]) - offset + 1) throw new ApiError(409, 'Сервер превысил размер диапазона.');
          handle.writeBytes(item.value);
          received += item.value.length;
        }
      } finally {
        try { handle.close(); } finally {
          await reader.cancel().catch(() => {});
          reader.releaseLock();
        }
      }
      record = { ...record, completedBytes: partial.size };
      onUpdate(record);
      if (received !== Number(match[2]) - offset + 1) throw new TypeError('Скачивание прервано; полученная часть сохранена.');
    }
  }
  let lastProgressAt = 0;
  let lastProgressBytes = 0;
  const task = !ranged ? File.createDownloadTask(
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
  ) : null;
  const downloaded = task ? await task.downloadAsync() : partial;
  if (!downloaded?.exists) throw new Error('Скачанный файл не найден.');
  if (initial.totalBytes > 0 && downloaded.size !== initial.totalBytes) {
    throw new Error('Файл скачан не полностью. Существующая копия сохранена.');
  }
  if (digest && await fileSha256(downloaded, assertActive) !== digest) {
    downloaded.delete();
    throw new ApiError(409, 'SHA-256 файла не совпадает; обновите список файлов.');
  }
  assertActive();
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

export async function fileSha256(file: File, assertActive: () => void = () => {}): Promise<string> {
  const hash = sha256.create();
  const handle = file.open();
  try {
    let remaining = file.size;
    while (remaining > 0) {
      assertActive();
      const bytes = handle.readBytes(Math.min(1024 * 1024, remaining));
      if (!bytes.length) throw new Error('Файл оборвался во время проверки SHA-256.');
      hash.update(bytes);
      remaining -= bytes.length;
      await new Promise(resolve => setTimeout(resolve, 0)); // Yield to UI and lease heartbeat.
    }
    return Array.from(hash.digest()).map(value => value.toString(16).padStart(2, '0')).join('');
  } finally { handle.close(); }
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
