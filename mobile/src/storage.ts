import { File, Paths } from 'expo-file-system';
import * as SecureStore from 'expo-secure-store';

import type { PersistedState } from './types';

const STATE_FILE = new File(Paths.document, 'cloud-storage-mobile.json');
const DEVICE_TOKEN_KEY = 'cloud-storage-device-token-v1';
const REMOTE_SESSION_KEY = 'cloud-storage-remote-session-v1';

const emptyState: PersistedState = { connection: null, transfers: [] };

export function loadState(): PersistedState {
  try {
    if (!STATE_FILE.exists) {
      return emptyState;
    }
    const parsed = JSON.parse(STATE_FILE.textSync()) as Partial<PersistedState>;
    return {
      connection: parsed.connection ?? null,
      transfers: Array.isArray(parsed.transfers) ? parsed.transfers : [],
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
