import { useMemo, useState } from 'react';
import { Platform, Pressable, StyleSheet, Text, View } from 'react-native';
import * as Device from 'expo-device';
import * as DocumentPicker from 'expo-document-picker';
import { File } from 'expo-file-system';

import { CloudApi } from '../api';
import { Brand, Button, Card, Field, Notice, Screen, Title } from '../components/Ui';
import { colors, radius, spacing } from '../theme';
import type { ConnectionProfile, PairingResult } from '../types';

type Mode = 'account' | 'code';

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
      setMode('code');
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : 'Файл доступа не импортирован.',
      );
    }
  };

  const connect = async () => {
    if (password.length < 10 || !deviceName.trim()) {
      setError('Введите пароль минимум из 10 символов и название телефона.');
      return;
    }
    if (mode === 'account' && username.trim().length < 3) {
      setError('Введите логин, созданный администратором.');
      return;
    }
    if (mode === 'code' && code.replace(/[-\s]/g, '').length !== 8) {
      setError('Введите восьмизначный одноразовый код.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const api = new CloudApi(serverUrl);
      const health = await api.health();
      const result =
        mode === 'account'
          ? await api.loginNewDevice(
              username.trim().toLocaleLowerCase(),
              password,
              deviceName.trim(),
              platformName,
            )
          : await api.redeemInvitation(code.trim(), password, deviceName.trim(), platformName);
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
        <ModeButton active={mode === 'code'} title="Одноразовый код" onPress={() => setMode('code')} />
      </View>
      <Card>
        <Field
          label="HTTPS-адрес сервера"
          value={serverUrl}
          onChangeText={setServerUrl}
          placeholder="https://ваш-сервер.zrok.io"
          keyboardType="url"
          autoCorrect={false}
        />
        {mode === 'account' ? (
          <Field
            label="Логин"
            value={username}
            onChangeText={setUsername}
            placeholder="ivan"
            autoCorrect={false}
          />
        ) : (
          <Field
            label="Код подключения"
            value={code}
            onChangeText={setCode}
            placeholder="ABCD-2345"
            autoCorrect={false}
            autoCapitalize="characters"
          />
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
        <Button title={mode === 'account' ? 'Войти' : 'Подключиться по коду'} onPress={connect} busy={busy} />
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
});
