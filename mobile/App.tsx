import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ActivityIndicator, Alert, StyleSheet, Text, View } from 'react-native';
import { File } from 'expo-file-system';
import * as Sharing from 'expo-sharing';
import { StatusBar } from 'expo-status-bar';

import { ApiError, CloudApi } from './src/api';
import { ensureBackgroundTransfersRegistered } from './src/background';
import { TabBar, type MainTab } from './src/components/TabBar';
import { Brand, Screen } from './src/components/Ui';
import { FilesScreen, type UploadAsset } from './src/screens/FilesScreen';
import { LoginScreen } from './src/screens/LoginScreen';
import { PendingScreen } from './src/screens/PendingScreen';
import { RemoteUnlockScreen } from './src/screens/RemoteUnlockScreen';
import { ServerControlScreen } from './src/screens/ServerControlScreen';
import { SettingsScreen } from './src/screens/SettingsScreen';
import { TransfersScreen } from './src/screens/TransfersScreen';
import {
  clearSecrets,
  loadDeviceToken,
  loadRemoteSession,
  loadState,
  saveDeviceToken,
  saveRemoteSession,
  saveState,
} from './src/storage';
import { colors } from './src/theme';
import {
  offlineDirectory,
  runDownloadTransfer,
  runUploadTransfer,
  safeFileName,
  uploadQueueDirectory,
} from './src/transfers';
import type {
  ConnectionProfile,
  FileEntry,
  MobileAdminOverview,
  PairingResult,
  SpaceRecord,
  TransferRecord,
} from './src/types';

type Stage = 'boot' | 'login' | 'pending' | 'unlock' | 'main';

function makeId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function joinPath(directory: string, name: string): string {
  return [directory, name].filter(Boolean).join('/');
}

export default function App() {
  const persisted = useMemo(() => loadState(), []);
  const [stage, setStage] = useState<Stage>('boot');
  const [connection, setConnection] = useState<ConnectionProfile | null>(persisted.connection);
  const [deviceToken, setDeviceToken] = useState('');
  const [remoteSession, setRemoteSession] = useState('');
  const [transfers, setTransfers] = useState<TransferRecord[]>(persisted.transfers);
  const [checking, setChecking] = useState(false);
  const [connectionError, setConnectionError] = useState('');
  const [spaces, setSpaces] = useState<SpaceRecord[]>([]);
  const [selectedSpaceId, setSelectedSpaceId] = useState('');
  const [directory, setDirectory] = useState('');
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [filesLoading, setFilesLoading] = useState(false);
  const [filesError, setFilesError] = useState('');
  const [tab, setTab] = useState<MainTab>('files');
  const [adminOverview, setAdminOverview] = useState<MobileAdminOverview | null>(null);
  const [adminLoading, setAdminLoading] = useState(false);
  const [adminError, setAdminError] = useState('');
  const activeTransfers = useRef(new Set<string>());
  const transferRef = useRef(transfers);
  const connectionRef = useRef(connection);

  useEffect(() => {
    transferRef.current = transfers;
  }, [transfers]);
  useEffect(() => {
    connectionRef.current = connection;
  }, [connection]);

  const api = useMemo(
    () => (connection ? new CloudApi(connection.serverUrl, deviceToken, remoteSession) : null),
    [connection, deviceToken, remoteSession],
  );

  const persistTransfers = useCallback((next: TransferRecord[]) => {
    transferRef.current = next;
    saveState({ connection: connectionRef.current, transfers: next });
  }, []);

  const replaceTransfer = useCallback(
    (updated: TransferRecord) => {
      setTransfers((current) => {
        const next = current.map((item) => (item.id === updated.id ? updated : item));
        persistTransfers(next);
        return next;
      });
    },
    [persistTransfers],
  );

  const enterMain = useCallback(async (candidate: CloudApi) => {
    try {
      const availableSpaces = await candidate.listSpaces();
      setSpaces(availableSpaces);
      setSelectedSpaceId((current) =>
        availableSpaces.some((space) => space.id === current)
          ? current
          : (availableSpaces[0]?.id ?? ''),
      );
      try {
        setAdminOverview(await candidate.mobileAdminOverview());
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 403) {
          setAdminError(error instanceof Error ? error.message : 'Управление сервером недоступно.');
        }
        setAdminOverview(null);
      }
      setConnectionError('');
      setStage('main');
    } catch (error) {
      if (error instanceof ApiError && error.needsInternetLogin) {
        setStage('unlock');
        return;
      }
      setConnectionError(error instanceof Error ? error.message : 'Сервер недоступен.');
      setStage('pending');
    }
  }, []);

  const checkDevice = useCallback(async () => {
    if (!connection || !api || !deviceToken) {
      setStage('login');
      return;
    }
    setChecking(true);
    setConnectionError('');
    try {
      const status = await api.pairingStatus();
      if (status.status === 'revoked') {
        throw new Error('Администратор отозвал это устройство. Подключите его заново.');
      }
      const updated = { ...connection, deviceStatus: status.status as ConnectionProfile['deviceStatus'] };
      setConnection(updated);
      connectionRef.current = updated;
      saveState({ connection: updated, transfers: transferRef.current });
      if (status.status === 'trusted') {
        await enterMain(api);
      } else {
        setStage('pending');
      }
    } catch (error) {
      setConnectionError(error instanceof Error ? error.message : 'Не удалось проверить устройство.');
      setStage('pending');
    } finally {
      setChecking(false);
    }
  }, [api, connection, deviceToken, enterMain]);

  useEffect(() => {
    void (async () => {
      const [storedDeviceToken, storedRemoteSession] = await Promise.all([
        loadDeviceToken(),
        loadRemoteSession(),
      ]);
      setDeviceToken(storedDeviceToken);
      setRemoteSession(storedRemoteSession);
      if (!persisted.connection || !storedDeviceToken) {
        setStage('login');
      }
      try {
        await ensureBackgroundTransfersRegistered();
      } catch {
        // The foreground queue still works if the OS restricts background tasks.
      }
    })();
  }, [persisted.connection]);

  useEffect(() => {
    if (stage === 'boot' && connection && deviceToken) void checkDevice();
  }, [stage, connection, deviceToken, checkDevice]);

  useEffect(() => {
    if (stage !== 'pending') return;
    const timer = setInterval(() => void checkDevice(), 15_000);
    return () => clearInterval(timer);
  }, [stage, checkDevice]);

  const connected = async (profile: ConnectionProfile, result: PairingResult) => {
    await saveDeviceToken(result.device_token);
    await saveRemoteSession('');
    setDeviceToken(result.device_token);
    setRemoteSession('');
    setConnection(profile);
    connectionRef.current = profile;
    saveState({ connection: profile, transfers: transferRef.current });
    setConnectionError('');
    setStage('pending');
  };

  const unlockInternet = async (password: string) => {
    if (!connection || !api) throw new Error('Подключение не найдено.');
    const session = await api.createRemoteSession(connection.username, password);
    await saveRemoteSession(session.session_token);
    setRemoteSession(session.session_token);
    const unlocked = new CloudApi(connection.serverUrl, deviceToken, session.session_token);
    await enterMain(unlocked);
  };

  const forget = async () => {
    await clearSecrets();
    const nextTransfers = transferRef.current.map((item) =>
      ['queued', 'running'].includes(item.status)
        ? { ...item, status: 'paused' as const, error: 'Подключение удалено' }
        : item,
    );
    setTransfers(nextTransfers);
    persistTransfers(nextTransfers);
    setConnection(null);
    connectionRef.current = null;
    saveState({ connection: null, transfers: nextTransfers });
    setDeviceToken('');
    setRemoteSession('');
    setSpaces([]);
    setAdminOverview(null);
    setEntries([]);
    setStage('login');
  };

  const lockInternet = async () => {
    await saveRemoteSession('');
    setRemoteSession('');
    setStage('unlock');
  };

  const loadEntries = useCallback(async () => {
    if (!api || !selectedSpaceId || stage !== 'main') return;
    setFilesLoading(true);
    setFilesError('');
    try {
      setEntries(await api.listEntries(selectedSpaceId, directory));
    } catch (error) {
      if (error instanceof ApiError && error.needsInternetLogin) setStage('unlock');
      setFilesError(error instanceof Error ? error.message : 'Список файлов недоступен.');
    } finally {
      setFilesLoading(false);
    }
  }, [api, selectedSpaceId, directory, stage]);

  useEffect(() => {
    void loadEntries();
  }, [loadEntries]);

  const processTransfer = useCallback(
    async (id: string) => {
      if (!api || activeTransfers.current.has(id)) return;
      const record = transferRef.current.find((item) => item.id === id);
      if (!record || !['queued', 'running'].includes(record.status)) return;
      activeTransfers.current.add(id);
      try {
        const completed =
          record.direction === 'upload'
            ? await runUploadTransfer(api, record, replaceTransfer)
            : await runDownloadTransfer(api, record, replaceTransfer);
        if (completed.direction === 'upload') void loadEntries();
        if (
          completed.direction === 'download' &&
          completed.status === 'completed' &&
          (await Sharing.isAvailableAsync())
        ) {
          await Sharing.shareAsync(completed.localUri);
        }
      } catch (error) {
        const latest = transferRef.current.find((item) => item.id === id) ?? record;
        replaceTransfer({
          ...latest,
          status: 'failed',
          error: error instanceof Error ? error.message : 'Передача не выполнена.',
        });
      } finally {
        activeTransfers.current.delete(id);
      }
    },
    [api, loadEntries, replaceTransfer],
  );

  useEffect(() => {
    if (stage !== 'main') return;
    transferRef.current
      .filter((item) => ['queued', 'running'].includes(item.status))
      .forEach((item) => void processTransfer(item.id));
  }, [stage, processTransfer, transfers]);

  const queueUpload = async (asset: UploadAsset) => {
    if (!selectedSpaceId) return;
    try {
      const id = makeId();
      const source = new File(asset.uri);
      const destination = new File(uploadQueueDirectory(), `${id}-${safeFileName(asset.name)}`);
      source.copy(destination, { overwrite: true });
      const record: TransferRecord = {
        id,
        direction: 'upload',
        status: 'queued',
        name: asset.name,
        logicalPath: joinPath(directory, asset.name),
        localUri: destination.uri,
        spaceId: selectedSpaceId,
        totalBytes: asset.size || destination.size,
        completedBytes: 0,
        createdAt: new Date().toISOString(),
      };
      const next = [record, ...transferRef.current];
      setTransfers(next);
      persistTransfers(next);
      setTab('transfers');
    } catch (error) {
      Alert.alert('Файл не добавлен', error instanceof Error ? error.message : 'Не удалось скопировать файл.');
    }
  };

  const queueDownload = (entry: FileEntry) => {
    if (!selectedSpaceId || entry.type !== 'file') return;
    const id = makeId();
    const destination = new File(offlineDirectory(), safeFileName(entry.name));
    const record: TransferRecord = {
      id,
      direction: 'download',
      status: 'queued',
      name: entry.name,
      logicalPath: joinPath(directory, entry.name),
      localUri: destination.uri,
      spaceId: selectedSpaceId,
      totalBytes: entry.size_bytes ?? 0,
      completedBytes: 0,
      createdAt: new Date().toISOString(),
    };
    const next = [record, ...transferRef.current];
    setTransfers(next);
    persistTransfers(next);
    setTab('transfers');
  };

  const deleteEntry = async (entry: FileEntry) => {
    if (!api || !selectedSpaceId) return;
    try {
      await api.deleteEntry(selectedSpaceId, joinPath(directory, entry.name), entry.type);
      await loadEntries();
    } catch (error) {
      setFilesError(error instanceof Error ? error.message : 'Удаление не выполнено.');
    }
  };

  const createDirectory = async (name: string) => {
    if (!api || !selectedSpaceId) return;
    try {
      await api.createDirectory(selectedSpaceId, joinPath(directory, name));
      await loadEntries();
    } catch (error) {
      setFilesError(error instanceof Error ? error.message : 'Папка не создана.');
    }
  };

  const retryTransfer = (id: string) => {
    const record = transferRef.current.find((item) => item.id === id);
    if (!record) return;
    replaceTransfer({ ...record, status: 'queued', error: undefined });
  };

  const clearCompleted = () => {
    const next = transferRef.current.filter((item) => item.status !== 'completed');
    setTransfers(next);
    persistTransfers(next);
  };

  const refreshAdmin = async () => {
    if (!api || !adminOverview) return;
    setAdminLoading(true);
    setAdminError('');
    try {
      setAdminOverview(await api.mobileAdminOverview());
    } catch (error) {
      if (error instanceof ApiError && error.needsInternetLogin) setStage('unlock');
      setAdminError(error instanceof Error ? error.message : 'Состояние сервера недоступно.');
    } finally {
      setAdminLoading(false);
    }
  };

  const setDeviceStatus = async (deviceId: string, status: 'approve' | 'revoke') => {
    if (!api) return;
    try {
      await api.mobileSetDeviceStatus(deviceId, status);
      await refreshAdmin();
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : 'Действие не выполнено.');
    }
  };

  const setServerMode = async (mode: 'normal' | 'read_only') => {
    if (!api) return;
    try {
      await api.mobileSetServerMode(
        mode,
        mode === 'read_only' ? 'Включено администратором с мобильного устройства' : '',
      );
      await refreshAdmin();
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : 'Режим сервера не изменён.');
    }
  };

  if (stage === 'boot') {
    return (
      <Screen>
        <StatusBar style="light" />
        <View style={styles.boot}>
          <Brand />
          <ActivityIndicator size="large" color={colors.red} />
          <Text style={styles.bootText}>Проверяем защищённое подключение…</Text>
        </View>
      </Screen>
    );
  }

  if (stage === 'login') {
    return <><StatusBar style="light" /><LoginScreen onConnected={connected} /></>;
  }

  if (!connection) return null;

  if (stage === 'pending') {
    return (
      <>
        <StatusBar style="light" />
        <PendingScreen
          profile={connection}
          checking={checking}
          error={connectionError}
          onCheck={() => void checkDevice()}
          onForget={() => void forget()}
        />
      </>
    );
  }

  if (stage === 'unlock') {
    return (
      <>
        <StatusBar style="light" />
        <RemoteUnlockScreen profile={connection} onUnlock={unlockInternet} onForget={forget} />
      </>
    );
  }

  const activeCount = transfers.filter((item) => ['queued', 'running'].includes(item.status)).length;
  return (
    <View style={styles.app}>
      <StatusBar style="light" />
      {tab === 'files' ? (
        <FilesScreen
          spaces={spaces}
          selectedSpaceId={selectedSpaceId}
          directory={directory}
          entries={entries}
          loading={filesLoading}
          error={filesError}
          onSelectSpace={(id) => { setSelectedSpaceId(id); setDirectory(''); }}
          onOpenDirectory={setDirectory}
          onRefresh={() => void loadEntries()}
          onUpload={(asset) => void queueUpload(asset)}
          onDownload={queueDownload}
          onDelete={(entry) => void deleteEntry(entry)}
          onCreateDirectory={(name) => void createDirectory(name)}
        />
      ) : null}
      {tab === 'transfers' ? (
        <TransfersScreen transfers={transfers} onRetry={retryTransfer} onClearCompleted={clearCompleted} />
      ) : null}
      {tab === 'settings' ? (
        <SettingsScreen
          profile={connection}
          onLockInternet={() => void lockInternet()}
          onForget={() =>
            Alert.alert('Забыть сервер?', 'Локальный токен будет удалён с телефона.', [
              { text: 'Отмена', style: 'cancel' },
              { text: 'Забыть', style: 'destructive', onPress: () => void forget() },
            ])
          }
        />
      ) : null}
      {tab === 'server' && adminOverview ? (
        <ServerControlScreen
          overview={adminOverview}
          loading={adminLoading}
          error={adminError}
          onRefresh={() => void refreshAdmin()}
          onApprove={(id) => void setDeviceStatus(id, 'approve')}
          onRevoke={(id) => void setDeviceStatus(id, 'revoke')}
          onSetMode={(mode) => void setServerMode(mode)}
        />
      ) : null}
      <TabBar tab={tab} onChange={setTab} badge={activeCount} admin={adminOverview !== null} />
    </View>
  );
}

const styles = StyleSheet.create({
  app: { flex: 1, backgroundColor: colors.background },
  boot: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: 24 },
  bootText: { color: colors.muted, textAlign: 'center' },
});
