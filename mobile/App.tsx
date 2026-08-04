import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ActivityIndicator, Alert, Share, StyleSheet, Text, View } from 'react-native';
import { File } from 'expo-file-system';
import * as LocalAuthentication from 'expo-local-authentication';
import * as Sharing from 'expo-sharing';
import { StatusBar } from 'expo-status-bar';

import { ApiError, CloudApi } from './src/api';
import { ensureBackgroundTransfersRegistered } from './src/background';
import { queuePhotoBackupBatch } from './src/photoBackup';
import { PreviewApp } from './src/PreviewApp';
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
  clearPhotoAssetIds,
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
  MobileAdminAction,
  MobileCreateUserInput,
  PairingResult,
  PhotoBackupSettings,
  SpaceRecord,
  TransferRecord,
} from './src/types';

type Stage = 'boot' | 'login' | 'pending' | 'unlock' | 'main';
type MobileVariant = 'client' | 'manager';

const mobileVariant: MobileVariant =
  process.env.EXPO_PUBLIC_APP_VARIANT === 'manager' ? 'manager' : 'client';

function makeId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function joinPath(directory: string, name: string): string {
  return [directory, name].filter(Boolean).join('/');
}

export default function App() {
  const previewScreen = process.env.EXPO_PUBLIC_SCREENSHOT_SCREEN;
  return previewScreen ? <PreviewApp screen={previewScreen} /> : <MainApp />;
}

function MainApp() {
  const persisted = useMemo(() => loadState(), []);
  const [stage, setStage] = useState<Stage>('boot');
  const [connection, setConnection] = useState<ConnectionProfile | null>(persisted.connection);
  const [deviceToken, setDeviceToken] = useState('');
  const [remoteSession, setRemoteSession] = useState('');
  const [transfers, setTransfers] = useState<TransferRecord[]>(persisted.transfers);
  const [photoBackup, setPhotoBackup] = useState<PhotoBackupSettings>(persisted.photoBackup);
  const [photoBusy, setPhotoBusy] = useState(false);
  const [photoMessage, setPhotoMessage] = useState('');
  const [checking, setChecking] = useState(false);
  const [connectionError, setConnectionError] = useState('');
  const [spaces, setSpaces] = useState<SpaceRecord[]>([]);
  const [selectedSpaceId, setSelectedSpaceId] = useState('');
  const [directory, setDirectory] = useState('');
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [filesLoading, setFilesLoading] = useState(false);
  const [filesError, setFilesError] = useState('');
  const [tab, setTab] = useState<MainTab>(mobileVariant === 'manager' ? 'server' : 'files');
  const [adminOverview, setAdminOverview] = useState<MobileAdminOverview | null>(null);
  const [adminLoading, setAdminLoading] = useState(false);
  const [adminError, setAdminError] = useState('');
  const activeTransfers = useRef(new Set<string>());
  const transferRef = useRef(transfers);
  const connectionRef = useRef(connection);
  const photoBackupRef = useRef(photoBackup);
  const photoRunActive = useRef(false);

  useEffect(() => {
    transferRef.current = transfers;
  }, [transfers]);
  useEffect(() => {
    connectionRef.current = connection;
  }, [connection]);
  useEffect(() => {
    photoBackupRef.current = photoBackup;
  }, [photoBackup]);

  const api = useMemo(
    () => (connection ? new CloudApi(connection.serverUrl, deviceToken, remoteSession) : null),
    [connection, deviceToken, remoteSession],
  );

  const persistTransfers = useCallback((next: TransferRecord[]) => {
    transferRef.current = next;
    saveState({ connection: connectionRef.current, transfers: next, photoBackup: photoBackupRef.current });
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
      if (mobileVariant === 'manager') {
        try {
          setAdminOverview(await candidate.mobileAdminOverview());
          setAdminError('');
        } catch (error) {
          if (error instanceof ApiError && error.status === 403) {
            setAdminError('Для этого приложения нужна учётная запись администратора.');
          } else {
            setAdminError(error instanceof Error ? error.message : 'Управление сервером недоступно.');
          }
          setAdminOverview(null);
        }
        setTab('server');
      } else {
        setAdminOverview(null);
        setTab('files');
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
      saveState({ connection: updated, transfers: transferRef.current, photoBackup: photoBackupRef.current });
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
    saveState({ connection: profile, transfers: transferRef.current, photoBackup: photoBackupRef.current });
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
    const disabledPhotoBackup = { ...photoBackupRef.current, enabled: false };
    photoBackupRef.current = disabledPhotoBackup;
    setPhotoBackup(disabledPhotoBackup);
    saveState({ connection: null, transfers: nextTransfers, photoBackup: disabledPhotoBackup });
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
      logicalPath: entry.logical_path ?? joinPath(directory, entry.name),
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
      await api.deleteEntry(selectedSpaceId, entry.logical_path ?? joinPath(directory, entry.name), entry.type);
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

  const searchFiles = async (query: string) => {
    if (!api || !selectedSpaceId) return;
    if (query.trim().length < 2) {
      await loadEntries();
      return;
    }
    setFilesLoading(true);
    setFilesError('');
    try {
      setEntries(await api.searchEntries(selectedSpaceId, query.trim(), directory));
    } catch (error) {
      setFilesError(error instanceof Error ? error.message : 'Поиск не выполнен.');
    } finally {
      setFilesLoading(false);
    }
  };

  const shareEntry = async (entry: FileEntry) => {
    if (!api || !selectedSpaceId) return;
    try {
      const shared = await api.createPublicShare(
        selectedSpaceId,
        entry.logical_path ?? joinPath(directory, entry.name),
        entry.type,
      );
      const url = `${api.serverUrl}${shared.url_path}`;
      await Share.share({ message: `Ссылка Cloud Storage действует 24 часа:\n${url}`, url });
    } catch (error) {
      setFilesError(error instanceof Error ? error.message : 'Ссылка не создана.');
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

  const savePhotoBackup = useCallback((next: PhotoBackupSettings) => {
    photoBackupRef.current = next;
    setPhotoBackup(next);
    saveState({
      connection: connectionRef.current,
      transfers: transferRef.current,
      photoBackup: next,
    });
  }, []);

  const runPhotoBackup = useCallback(async (requestPermission = false) => {
    if (photoRunActive.current) return;
    const preferredSpace = photoBackupRef.current.spaceId ||
      spaces.find((space) => space.kind === 'personal')?.id || selectedSpaceId || spaces[0]?.id || '';
    const settings = { ...photoBackupRef.current, spaceId: preferredSpace };
    if (!settings.enabled || !preferredSpace) return;
    photoRunActive.current = true;
    setPhotoBusy(true);
    setPhotoMessage('');
    try {
      const result = await queuePhotoBackupBatch(
        { connection: connectionRef.current, transfers: transferRef.current, photoBackup: settings },
        requestPermission ? 25 : 5,
        requestPermission,
      );
      transferRef.current = result.state.transfers;
      photoBackupRef.current = result.state.photoBackup;
      setTransfers(result.state.transfers);
      setPhotoBackup(result.state.photoBackup);
      saveState(result.state);
      setPhotoMessage(result.message);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Не удалось проверить медиатеку.';
      const failed = { ...settings, lastError: message, lastScanAt: new Date().toISOString() };
      savePhotoBackup(failed);
      setPhotoMessage(message);
    } finally {
      photoRunActive.current = false;
      setPhotoBusy(false);
    }
  }, [savePhotoBackup, selectedSpaceId, spaces]);

  const updatePhotoBackup = async (patch: Partial<PhotoBackupSettings>) => {
    const enabling = patch.enabled === true && !photoBackupRef.current.enabled;
    const next = {
      ...photoBackupRef.current,
      ...patch,
      spaceId: patch.spaceId ?? photoBackupRef.current.spaceId ?? selectedSpaceId,
    };
    savePhotoBackup(next);
    if (enabling) await runPhotoBackup(true);
  };

  const resetPhotoBackupIndex = () => {
    clearPhotoAssetIds();
    const next = {
      ...photoBackupRef.current,
      scanOffset: 0,
      initialScanComplete: false,
      queuedCount: 0,
      lastScanAt: '',
      lastError: undefined,
    };
    savePhotoBackup(next);
    setPhotoMessage('Индекс фотографий очищен. Следующая проверка начнётся заново.');
  };

  useEffect(() => {
    if (mobileVariant !== 'client' || stage !== 'main' || !photoBackup.enabled || spaces.length === 0) return;
    if (!photoBackup.spaceId) {
      const spaceId = spaces.find((space) => space.kind === 'personal')?.id ?? spaces[0]?.id ?? '';
      if (spaceId) savePhotoBackup({ ...photoBackup, spaceId });
    }
    void runPhotoBackup(false);
    const timer = setInterval(() => void runPhotoBackup(false), 5 * 60_000);
    return () => clearInterval(timer);
  }, [stage, photoBackup.enabled, photoBackup.spaceId, spaces, runPhotoBackup, savePhotoBackup]);

  const refreshAdmin = async () => {
    if (!api) return;
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

  const confirmAdminAction = async (action: MobileAdminAction, password: string) => {
    if (!api) throw new Error('Сервер недоступен.');
    const [compatible, enrolled] = await Promise.all([
      LocalAuthentication.hasHardwareAsync(),
      LocalAuthentication.isEnrolledAsync(),
    ]);
    if (compatible && enrolled) {
      const local = await LocalAuthentication.authenticateAsync({
        promptMessage: 'Подтвердите действие администратора',
        cancelLabel: 'Отмена',
        disableDeviceFallback: false,
      });
      if (!local.success) throw new Error('Локальное подтверждение отменено.');
    }
    const result = await api.mobileConfirmAdminAction(password, action);
    return result.confirmation_token;
  };

  const setDeviceStatus = async (
    deviceId: string,
    status: 'approve' | 'revoke',
    password: string,
  ) => {
    if (!api) return;
    try {
      const token = await confirmAdminAction(
        status === 'approve' ? 'device.approve' : 'device.revoke',
        password,
      );
      await api.mobileSetDeviceStatus(deviceId, status, token);
      await refreshAdmin();
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : 'Действие не выполнено.');
    }
  };

  const setServerMode = async (mode: 'normal' | 'read_only', password: string) => {
    if (!api) return;
    try {
      const token = await confirmAdminAction(
        mode === 'read_only' ? 'server.read_only' : 'server.normal',
        password,
      );
      await api.mobileSetServerMode(
        mode,
        mode === 'read_only' ? 'Включено администратором с мобильного устройства' : '',
        token,
      );
      await refreshAdmin();
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : 'Режим сервера не изменён.');
    }
  };

  const createMobileUser = async (input: MobileCreateUserInput, adminPassword: string) => {
    if (!api) return;
    try {
      const token = await confirmAdminAction('user.create', adminPassword);
      await api.mobileCreateUser(input, token);
      await refreshAdmin();
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : 'Пользователь не создан.');
      throw error;
    }
  };

  const setMobileUserPassword = async (userId: string, password: string, adminPassword: string) => {
    if (!api) return;
    try {
      const token = await confirmAdminAction('user.password', adminPassword);
      await api.mobileSetUserPassword(userId, password, token);
      await refreshAdmin();
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : 'Пароль пользователя не изменён.');
      throw error;
    }
  };

  const setMobileUserEnabled = async (userId: string, enabled: boolean, adminPassword: string) => {
    if (!api) return;
    try {
      const token = await confirmAdminAction(enabled ? 'user.enable' : 'user.disable', adminPassword);
      await api.mobileSetUserEnabled(userId, enabled, token);
      await refreshAdmin();
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : 'Состояние пользователя не изменено.');
      throw error;
    }
  };

  const runMobileDiagnostics = async (kind: 'quick' | 'full', adminPassword: string) => {
    if (!api) return;
    const token = await confirmAdminAction(kind === 'full' ? 'diagnostics.full' : 'diagnostics.quick', adminPassword);
    await api.mobileRunDiagnostics(kind, token);
    await refreshAdmin();
  };

  const runMobileBackup = async (rootId: string, adminPassword: string) => {
    if (!api) return;
    const token = await confirmAdminAction('backup.run', adminPassword);
    await api.mobileRunBackup(rootId, token);
    await refreshAdmin();
  };

  const setMobileStorageWrite = async (rootId: string, enabled: boolean, adminPassword: string) => {
    if (!api) return;
    const token = await confirmAdminAction(enabled ? 'storage.write.resume' : 'storage.write.pause', adminPassword);
    await api.mobileSetStorageWrite(rootId, enabled, token);
    await refreshAdmin();
  };

  const setMobileAutomation = async (enabled: boolean, adminPassword: string) => {
    if (!api) return;
    const token = await confirmAdminAction(enabled ? 'automation.resume' : 'automation.pause', adminPassword);
    await api.mobileSetAutomation(enabled, token);
    await refreshAdmin();
  };

  const restartMobileTunnel = async (adminPassword: string) => {
    if (!api) return;
    const token = await confirmAdminAction('tunnel.restart', adminPassword);
    await api.mobileRestartTunnel('zrok', token);
    await refreshAdmin();
  };

  const restartMobileCore = async (adminPassword: string) => {
    if (!api) return;
    const token = await confirmAdminAction('core.restart', adminPassword);
    await api.mobileRestartCore(token);
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
      {mobileVariant === 'client' && tab === 'files' ? (
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
          onSearch={(query) => void searchFiles(query)}
          onShare={(entry) => void shareEntry(entry)}
        />
      ) : null}
      {mobileVariant === 'client' && tab === 'transfers' ? (
        <TransfersScreen transfers={transfers} onRetry={retryTransfer} onClearCompleted={clearCompleted} />
      ) : null}
      {tab === 'settings' ? (
        <SettingsScreen
          profile={connection}
          spaces={spaces}
          photoBackup={photoBackup}
          photoBusy={photoBusy}
          photoMessage={photoMessage}
          onPhotoBackupChange={(patch) => void updatePhotoBackup(patch)}
          onRunPhotoBackup={() => void runPhotoBackup(true)}
          onResetPhotoIndex={resetPhotoBackupIndex}
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
          onApprove={(id, password) => setDeviceStatus(id, 'approve', password)}
          onRevoke={(id, password) => setDeviceStatus(id, 'revoke', password)}
          onSetMode={setServerMode}
          onCreateUser={createMobileUser}
          onSetUserPassword={setMobileUserPassword}
          onSetUserEnabled={setMobileUserEnabled}
          onRunDiagnostics={runMobileDiagnostics}
          onRunBackup={runMobileBackup}
          onSetStorageWrite={setMobileStorageWrite}
          onSetAutomation={setMobileAutomation}
          onRestartTunnel={restartMobileTunnel}
          onRestartCore={restartMobileCore}
        />
      ) : null}
      {mobileVariant === 'manager' && tab === 'server' && !adminOverview ? (
        <Screen>
          <Brand />
          <Text style={styles.managerMessage}>
            {adminLoading ? 'Загружаем управление сервером…' : (adminError || 'Управление сервером недоступно.')}
          </Text>
        </Screen>
      ) : null}
      <TabBar
        tab={tab}
        onChange={setTab}
        badge={activeCount}
        admin={adminOverview !== null}
        mode={mobileVariant}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  app: { flex: 1, backgroundColor: colors.background },
  boot: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: 24 },
  bootText: { color: colors.muted, textAlign: 'center' },
  managerMessage: { color: colors.muted, fontSize: 16, lineHeight: 24, textAlign: 'center' },
});
