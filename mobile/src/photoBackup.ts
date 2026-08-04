import * as Battery from 'expo-battery';
import { File } from 'expo-file-system';
import {
  AssetField,
  MediaType,
  Query,
  getPermissionsAsync,
  requestPermissionsAsync,
} from 'expo-media-library';
import * as Network from 'expo-network';

import {
  loadPhotoAssetIds,
  savePhotoAssetIds,
} from './storage';
import { safeFileName, uploadQueueDirectory } from './transfers';
import type { PersistedState, PhotoBackupSettings, TransferRecord } from './types';

const PHOTO_BATCH_SIZE = 50;

export interface PhotoBackupResult {
  state: PersistedState;
  queued: number;
  examined: number;
  message: string;
}

function makeId(): string {
  return `photo-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function joinPath(...parts: string[]): string {
  return parts.map((item) => item.trim().replace(/^\/+|\/+$/g, '')).filter(Boolean).join('/');
}

function allowedNetwork(type: Network.NetworkStateType | undefined): boolean {
  return [
    Network.NetworkStateType.WIFI,
    Network.NetworkStateType.ETHERNET,
    Network.NetworkStateType.VPN,
  ].includes(type as Network.NetworkStateType);
}

async function checkConditions(settings: PhotoBackupSettings): Promise<string> {
  const network = await Network.getNetworkStateAsync();
  if (!network.isConnected || network.isInternetReachable === false) {
    return 'Нет доступного сетевого подключения.';
  }
  if (settings.wifiOnly && !allowedNetwork(network.type)) {
    return 'Ожидается Wi‑Fi, Ethernet или VPN.';
  }
  if (settings.chargingOnly) {
    const power = await Battery.getPowerStateAsync();
    if (![Battery.BatteryState.CHARGING, Battery.BatteryState.FULL, Battery.BatteryState.NOT_CHARGING]
      .includes(power.batteryState)) {
      return 'Ожидается подключение телефона к питанию.';
    }
  }
  return '';
}

export async function ensurePhotoPermission(includeVideos: boolean): Promise<void> {
  const granular: Array<'photo' | 'video'> = includeVideos ? ['photo', 'video'] : ['photo'];
  let permission = await getPermissionsAsync(false, granular);
  if (!permission.granted) permission = await requestPermissionsAsync(false, granular);
  if (!permission.granted) {
    throw new Error('Доступ к медиатеке не разрешён. Откройте системные настройки телефона.');
  }
}

export async function queuePhotoBackupBatch(
  initial: PersistedState,
  maxAssets = 10,
  requestPermission = false,
): Promise<PhotoBackupResult> {
  const settings = initial.photoBackup;
  if (!settings.enabled || !settings.spaceId) {
    return { state: initial, queued: 0, examined: 0, message: 'Фотосинхронизация выключена.' };
  }
  const blocked = await checkConditions(settings);
  if (blocked) {
    return { state: initial, queued: 0, examined: 0, message: blocked };
  }
  if (requestPermission) {
    await ensurePhotoPermission(settings.includeVideos);
  } else {
    const granular: Array<'photo' | 'video'> = settings.includeVideos
      ? ['photo', 'video']
      : ['photo'];
    const permission = await getPermissionsAsync(false, granular);
    if (!permission.granted) {
      return {
        state: initial,
        queued: 0,
        examined: 0,
        message: 'Ожидается разрешение на доступ к медиатеке.',
      };
    }
  }

  const offset = settings.initialScanComplete ? 0 : settings.scanOffset;
  let query = new Query()
    .orderBy({ key: AssetField.CREATION_TIME, ascending: false })
    .offset(offset)
    .limit(PHOTO_BATCH_SIZE);
  query = settings.includeVideos
    ? query.within(AssetField.MEDIA_TYPE, [MediaType.IMAGE, MediaType.VIDEO])
    : query.eq(AssetField.MEDIA_TYPE, MediaType.IMAGE);
  const assets = await query.exe();
  const known = loadPhotoAssetIds();
  const alreadyQueued = new Set(
    initial.transfers.map((item) => item.sourceAssetId).filter((item): item is string => Boolean(item)),
  );
  const transfers: TransferRecord[] = [...initial.transfers];
  let queued = 0;
  let examined = 0;

  for (const asset of assets) {
    examined += 1;
    if (known.has(asset.id) || alreadyQueued.has(asset.id)) continue;
    if (queued >= maxAssets) break;
    try {
      const [uri, filename, createdAt, mediaType] = await Promise.all([
        asset.getUri(),
        asset.getFilename(),
        asset.getCreationTime(),
        asset.getMediaType(),
      ]);
      const date = new Date(createdAt ?? Date.now());
      const year = String(date.getFullYear());
      const month = String(date.getMonth() + 1).padStart(2, '0');
      const suffix = asset.id.replace(/[^a-z0-9]/gi, '').slice(-8) || String(date.getTime());
      const baseName = safeFileName(filename || `${mediaType}-${date.getTime()}`);
      const dot = baseName.lastIndexOf('.');
      const uniqueName = dot > 0
        ? `${baseName.slice(0, dot)}-${suffix}${baseName.slice(dot)}`
        : `${baseName}-${suffix}`;
      const id = makeId();
      const source = new File(uri);
      const queuedFile = new File(uploadQueueDirectory(), `${id}-${uniqueName}`);
      source.copy(queuedFile, { overwrite: true });
      transfers.unshift({
        id,
        direction: 'upload',
        status: 'queued',
        name: uniqueName,
        logicalPath: joinPath(settings.destination, year, month, uniqueName),
        localUri: queuedFile.uri,
        spaceId: settings.spaceId,
        totalBytes: queuedFile.size,
        completedBytes: 0,
        sourceAssetId: asset.id,
        createdAt: new Date().toISOString(),
      });
      known.add(asset.id);
      alreadyQueued.add(asset.id);
      queued += 1;
    } catch {
      // An iCloud-only or temporarily locked asset is left unknown and retried later.
    }
  }

  savePhotoAssetIds(known);
  const reachedEnd = assets.length < PHOTO_BATCH_SIZE;
  const nextSettings: PhotoBackupSettings = {
    ...settings,
    scanOffset: settings.initialScanComplete
      ? 0
      : reachedEnd
        ? 0
        : offset + examined,
    initialScanComplete: settings.initialScanComplete || reachedEnd,
    queuedCount: settings.queuedCount + queued,
    lastScanAt: new Date().toISOString(),
    lastError: undefined,
  };
  const state: PersistedState = { ...initial, transfers, photoBackup: nextSettings };
  return {
    state,
    queued,
    examined,
    message: queued > 0
      ? `Добавлено в очередь: ${queued}.`
      : reachedEnd || settings.initialScanComplete
        ? 'Новых фотографий и видео не найдено.'
        : 'Проверена следующая часть медиатеки.',
  };
}
