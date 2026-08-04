import { File, Paths } from 'expo-file-system';
import * as SecureStore from 'expo-secure-store';

import type { PersistedState, PhotoBackupSettings } from './types';

const STATE_FILE = new File(Paths.document, 'cloud-storage-mobile.json');
const PHOTO_INDEX_FILE = new File(Paths.document, 'cloud-storage-photo-index.json');
const DEVICE_TOKEN_KEY = 'cloud-storage-device-token-v1';
const REMOTE_SESSION_KEY = 'cloud-storage-remote-session-v1';

export const defaultPhotoBackup: PhotoBackupSettings = {
  enabled: false,
  wifiOnly: true,
  chargingOnly: false,
  includeVideos: true,
  destination: 'Фото с телефона',
  spaceId: '',
  scanOffset: 0,
  initialScanComplete: false,
  queuedCount: 0,
  lastScanAt: '',
};

const emptyState: PersistedState = {
  connection: null,
  transfers: [],
  photoBackup: defaultPhotoBackup,
};

export function loadState(): PersistedState {
  try {
    if (!STATE_FILE.exists) {
      return emptyState;
    }
    const parsed = JSON.parse(STATE_FILE.textSync()) as Partial<PersistedState>;
    return {
      connection: parsed.connection ?? null,
      transfers: Array.isArray(parsed.transfers) ? parsed.transfers : [],
      photoBackup: { ...defaultPhotoBackup, ...(parsed.photoBackup ?? {}) },
    };
  } catch {
    return emptyState;
  }
}

export function saveState(state: PersistedState): void {
  if (!STATE_FILE.exists) {
    STATE_FILE.create({ intermediates: true });
  }
  STATE_FILE.write(JSON.stringify(state));
}

export function loadPhotoAssetIds(): Set<string> {
  try {
    if (!PHOTO_INDEX_FILE.exists) return new Set();
    const parsed = JSON.parse(PHOTO_INDEX_FILE.textSync()) as { assetIds?: string[] };
    return new Set(Array.isArray(parsed.assetIds) ? parsed.assetIds : []);
  } catch {
    return new Set();
  }
}

export function savePhotoAssetIds(assetIds: Set<string>): void {
  if (!PHOTO_INDEX_FILE.exists) PHOTO_INDEX_FILE.create({ intermediates: true });
  PHOTO_INDEX_FILE.write(JSON.stringify({ assetIds: [...assetIds] }));
}

export function clearPhotoAssetIds(): void {
  if (PHOTO_INDEX_FILE.exists) PHOTO_INDEX_FILE.delete();
}

export async function loadDeviceToken(): Promise<string> {
  return (await SecureStore.getItemAsync(DEVICE_TOKEN_KEY)) ?? '';
}

export async function saveDeviceToken(token: string): Promise<void> {
  await SecureStore.setItemAsync(DEVICE_TOKEN_KEY, token, {
    keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
  });
}

export async function loadRemoteSession(): Promise<string> {
  return (await SecureStore.getItemAsync(REMOTE_SESSION_KEY)) ?? '';
}

export async function saveRemoteSession(token: string): Promise<void> {
  await SecureStore.setItemAsync(REMOTE_SESSION_KEY, token, {
    keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
  });
}

export async function clearSecrets(): Promise<void> {
  await Promise.all([
    SecureStore.deleteItemAsync(DEVICE_TOKEN_KEY),
    SecureStore.deleteItemAsync(REMOTE_SESSION_KEY),
  ]);
}
