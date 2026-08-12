import { useMemo, useState } from 'react';
import { Platform, Pressable, StyleSheet, Text, View } from 'react-native';
import { CameraView, useCameraPermissions, type BarcodeScanningResult } from 'expo-camera';
import * as Device from 'expo-device';
import * as DocumentPicker from 'expo-document-picker';
import { File } from 'expo-file-system';

import { CloudApi } from '../api';
import { Brand, Button, Card, Field, Notice, Screen, Title } from '../components/Ui';
import { colors, radius, spacing } from '../theme';
import type { ConnectionProfile, PairingResult } from '../types';

type Mode = 'account' | 'link' | 'qr';

type Invitation = {
  code: string;
  serverUrl: string;
  username: string;
};

function parseInvitation(value: string): Invitation {
  const candidate = value.trim();
  if (!candidate.toLocaleLowerCase().startsWith('cloudstorage://pair?')) {
    throw new Error('Нужна ссылка cloudstorage://pair из Server Manager.');
  }
  const parsed = new URL(candidate);
  const code = (parsed.searchParams.get('code') ?? '').trim().toLocaleUpperCase();
  const serverUrl = (parsed.searchParams.get('server') ?? '').trim().replace(/\/$/, '');
  const username = (parsed.searchParams.get('username') ?? '').trim();
  if (code.replace(/[-\s]/g, '').length !== 8 || !/^https?:\/\//i.test(serverUrl)) {
    throw new Error('Ссылка подключения повреждена или не содержит адрес сервера.');
  }
  return { code, serverUrl, username };
}

export function LoginScreen({
  onConnected,
}: {
  onConnected: (profile: ConnectionProfile, result: PairingResult) => Promise<void>;
}) {
  const [mode, setMode] = useState<Mode>('account');
  const [serverUrl, setServerUrl] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [link, setLink] = useState('');
  const [qrScanned, setQrScanned] = useState(false);
  const [cameraPermission, requestCameraPermission] = useCameraPermissions();
  const [deviceName, setDeviceName] = useState(Device.deviceName ?? 'Мой телефон');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const platformName = useMemo(() => (Platform.OS === 'ios' ? 'iOS' : 'Android'), []);

  const importAccessFile = async () => {
    setError('');
    try {
      const result = await DocumentPicker.getDocumentAsync({
        copyToCacheDirectory: true,
        multiple: false,
        type: 'application/json',
      });
      const asset = result.assets?.[0];
      if (result.canceled || !asset) return;
      if ((asset.size ?? 0) > 64 * 1024) throw new Error('Файл доступа слишком большой.');
      const value = JSON.parse(await new File(asset.uri).text()) as {
        format?: string;
        addresses?: unknown;
        username?: unknown;
        one_time_code?: unknown;
        login_link?: unknown;
      };
      if (value.format !== 'cloud-storage-access-v1') {
        throw new Error('Это не файл доступа Cloud Storage.');
      }
      if (!Array.isArray(value.addresses) || !value.addresses[0]) {
        throw new Error('В файле нет адреса сервера.');
      }
      const importedCode = String(value.one_time_code ?? '').trim();
      const importedUsername = String(value.username ?? '').trim();
      if (!importedCode || !importedUsername) {
        throw new Error('В файле не хватает данных входа.');
      }
      setServerUrl(String(value.addresses[0]));
      setUsername(importedUsername);
      setCode(importedCode);
      setLink(
        typeof value.login_link === 'string' && value.login_link
          ? value.login_link
          : `cloudstorage://pair?code=${encodeURIComponent(importedCode)}&server=${encodeURIComponent(String(value.addresses[0]))}&username=${encodeURIComponent(importedUsername)}`,
      );
      setMode('link');
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : 'Файл доступа не импортирован.',
      );
    }
  };

  const applyInvitation = (value: string) => {
    const invitation = parseInvitation(value);
    setServerUrl(invitation.serverUrl);
    setCode(invitation.code);
    if (invitation.username) setUsername(invitation.username);
    setLink(value.trim());
    return invitation;
  };

  const scanQr = ({ data }: BarcodeScanningResult) => {
    if (qrScanned) return;
    try {
      applyInvitation(data);
      setQrScanned(true);
      setError('');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'QR-код не распознан.');
    }
  };

  const connect = async () => {
    if (password.length < 10 || !deviceName.trim()) {
      setError('Введите пароль минимум из 10 символов и название телефона.');
      return;
    }
    let targetServer = serverUrl;
    let targetCode = code;
    if (mode !== 'account') {
      try {
        const invitation = applyInvitation(link);
        targetServer = invitation.serverUrl;
        targetCode = invitation.code;
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : 'Приглашение не распознано.');
        return;
      }
    }
    if (mode === 'account' && username.trim().length < 3) {
      setError('Введите логин, созданный администратором.');
      return;
    }
    if (mode !== 'account' && targetCode.replace(/[-\s]/g, '').length !== 8) {
      setError('В приглашении нет рабочего одноразового кода.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const api = new CloudApi(targetServer);
      const health = await api.health();
      const result =
        mode === 'account'
          ? await api.loginNewDevice(
              username.trim().toLocaleLowerCase(),
              password,
              deviceName.trim(),
              platformName,
            )
          : await api.redeemInvitation(
              targetCode.trim(),
              password,
              deviceName.trim(),
              platformName,
            );
      await onConnected(
        {
          serverUrl: api.serverUrl,
          serverName: health.server_name ?? 'Домашнее облако',
          username: result.device.username,
          deviceId: result.device.id,
          deviceName: result.device.name,
          deviceStatus: result.device.status,
        },
        result,
      );
      setPassword('');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Подключение не выполнено.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Screen>
      <Brand />
      <Title subtitle="Один аккаунт работает на компьютерах Android и iPhone. Новый телефон появится в Server Manager и будет ждать подтверждения.">
        Подключить телефон
      </Title>
      <View style={styles.switcher}>
        <ModeButton active={mode === 'account'} title="Логин и пароль" onPress={() => setMode('account')} />
        <ModeButton active={mode === 'link'} title="Ссылка" onPress={() => setMode('link')} />
        <ModeButton active={mode === 'qr'} title="QR-code" onPress={() => setMode('qr')} />
      </View>
      <Card>
        {mode === 'account' ? (
          <>
            <Field
              label="HTTPS-адрес сервера"
              value={serverUrl}
              onChangeText={setServerUrl}
              placeholder="https://ваш-сервер.zrok.io"
              keyboardType="url"
              autoCorrect={false}
            />
            <Field
              label="Логин"
              value={username}
              onChangeText={setUsername}
              placeholder="ivan"
              autoCorrect={false}
            />
          </>
        ) : mode === 'link' ? (
          <Field
            label="Ссылка из Server Manager"
            value={link}
            onChangeText={(value) => {
              setLink(value);
              setQrScanned(false);
            }}
            placeholder="cloudstorage://pair?…"
            keyboardType="url"
            autoCorrect={false}
          />
        ) : cameraPermission?.granted && !qrScanned ? (
          <View style={styles.cameraBox}>
            <CameraView
              style={styles.camera}
              facing="back"
              barcodeScannerSettings={{ barcodeTypes: ['qr'] }}
              onBarcodeScanned={scanQr}
            />
            <Text style={styles.cameraHelp}>Наведите камеру на QR в Server Manager</Text>
          </View>
        ) : qrScanned ? (
          <Notice tone="green">QR распознан. Адрес и одноразовый код готовы.</Notice>
        ) : (
          <Button title="Разрешить камеру и сканировать QR" onPress={() => void requestCameraPermission()} />
        )}
        <Field
          label="Пароль"
          value={password}
          onChangeText={setPassword}
          placeholder="Минимум 10 символов"
          secureTextEntry
          textContentType="password"
        />
        <Field
          label="Название телефона"
          value={deviceName}
          onChangeText={setDeviceName}
          placeholder="Телефон Ивана"
          autoCapitalize="sentences"
        />
        {error ? <Notice tone="red">{error}</Notice> : null}
        <Button title="Импортировать файл входа" onPress={importAccessFile} />
        {mode === 'qr' && qrScanned ? (
          <Button
            title="Сканировать другой QR"
            secondary
            onPress={() => {
              setQrScanned(false);
              setLink('');
            }}
          />
        ) : null}
        <Button
          title={mode === 'account' ? 'Войти' : mode === 'link' ? 'Подключиться по ссылке' : 'Подключиться по QR'}
          onPress={connect}
          busy={busy}
          disabled={mode === 'qr' && !qrScanned}
        />
      </Card>
      <Notice tone="blue">
        Пароль не сохраняется в приложении. Токен устройства хранится в Android Keystore или iOS Keychain. Для доступа через интернет используйте публичный HTTPS-адрес zrok.
      </Notice>
    </Screen>
  );
}

function ModeButton({ active, title, onPress }: { active: boolean; title: string; onPress: () => void }) {
  return (
    <Pressable onPress={onPress} style={[styles.modeButton, active && styles.modeButtonActive]}>
      <Text style={[styles.modeText, active && styles.modeTextActive]}>{title}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  switcher: {
    flexDirection: 'row',
    padding: 4,
    borderRadius: radius.md,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
  },
  modeButton: { flex: 1, borderRadius: radius.sm, padding: spacing.sm, alignItems: 'center' },
  modeButtonActive: { backgroundColor: colors.red },
  modeText: { color: colors.muted, fontWeight: '700' },
  modeTextActive: { color: colors.white },
  cameraBox: {
    overflow: 'hidden',
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surfaceAlt,
  },
  camera: { width: '100%', height: 280 },
  cameraHelp: { color: colors.muted, textAlign: 'center', padding: spacing.sm },
});
