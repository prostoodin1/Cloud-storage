import { Alert, Pressable, StyleSheet, Switch, Text, View } from 'react-native';

import { Button, Card, Field, Notice, Screen, Title } from '../components/Ui';
import { colors, spacing } from '../theme';
import type { ConnectionProfile, PhotoBackupSettings, SpaceRecord } from '../types';

function SettingSwitch({
  label,
  description,
  value,
  disabled = false,
  onChange,
}: {
  label: string;
  description: string;
  value: boolean;
  disabled?: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <View style={[styles.switchRow, disabled && styles.disabled]}>
      <View style={styles.switchText}>
        <Text style={styles.switchLabel}>{label}</Text>
        <Text style={styles.muted}>{description}</Text>
      </View>
      <Switch
        disabled={disabled}
        value={value}
        onValueChange={onChange}
        trackColor={{ false: colors.border, true: colors.redDark }}
        thumbColor={value ? colors.red : colors.muted}
      />
    </View>
  );
}

export function SettingsScreen({
  profile,
  spaces,
  photoBackup,
  photoBusy,
  photoMessage,
  onPhotoBackupChange,
  onRunPhotoBackup,
  onResetPhotoIndex,
  onLockInternet,
  onForget,
}: {
  profile: ConnectionProfile;
  spaces: SpaceRecord[];
  photoBackup: PhotoBackupSettings;
  photoBusy: boolean;
  photoMessage: string;
  onPhotoBackupChange: (patch: Partial<PhotoBackupSettings>) => void;
  onRunPhotoBackup: () => void;
  onResetPhotoIndex: () => void;
  onLockInternet: () => void;
  onForget: () => void;
}) {
  return (
    <Screen>
      <Title subtitle="Подключение, фотокопирование и безопасность">Настройки</Title>
      <Card>
        <Text style={styles.label}>СЕРВЕР</Text>
        <Text style={styles.value}>{profile.serverName}</Text>
        <Text style={styles.muted}>{profile.serverUrl}</Text>
        <Text style={styles.label}>ПОЛЬЗОВАТЕЛЬ</Text>
        <Text style={styles.value}>@{profile.username}</Text>
        <Text style={styles.label}>УСТРОЙСТВО</Text>
        <Text style={styles.value}>{profile.deviceName}</Text>
        <Text style={styles.trusted}>● Подтверждено администратором</Text>
      </Card>

      <Card>
        <Text style={styles.section}>Фото с телефона</Text>
        <Text style={styles.muted}>
          Новые фото и видео копируются в очередь Cloud Storage. Уже обработанные файлы не
          дублируются, а отправка продолжится после восстановления связи.
        </Text>
        <SettingSwitch
          label="Автоматическая копия"
          description="Проверять медиатеку при запуске и затем каждые пять минут."
          value={photoBackup.enabled}
          onChange={(enabled) => onPhotoBackupChange({ enabled })}
        />
        <SettingSwitch
          label="Только Wi‑Fi / Ethernet / VPN"
          description="Не расходовать мобильный трафик."
          value={photoBackup.wifiOnly}
          disabled={!photoBackup.enabled}
          onChange={(wifiOnly) => onPhotoBackupChange({ wifiOnly })}
        />
        <SettingSwitch
          label="Только при питании"
          description="Запускать копирование, когда телефон подключён к зарядке."
          value={photoBackup.chargingOnly}
          disabled={!photoBackup.enabled}
          onChange={(chargingOnly) => onPhotoBackupChange({ chargingOnly })}
        />
        <SettingSwitch
          label="Включать видео"
          description="Фото копируются всегда; видео можно исключить."
          value={photoBackup.includeVideos}
          disabled={!photoBackup.enabled}
          onChange={(includeVideos) => onPhotoBackupChange({ includeVideos })}
        />
        <Text style={styles.label}>ХРАНИЛИЩЕ</Text>
        <View style={styles.spaces}>
          {spaces.map((space) => (
            <Pressable
              key={space.id}
              disabled={!photoBackup.enabled}
              onPress={() => onPhotoBackupChange({ spaceId: space.id })}
              style={[
                styles.spacePill,
                photoBackup.spaceId === space.id && styles.spacePillActive,
                !photoBackup.enabled && styles.disabled,
              ]}
            >
              <Text style={styles.spaceText}>{space.name}</Text>
            </Pressable>
          ))}
        </View>
        <Field
          label="Папка назначения"
          value={photoBackup.destination}
          editable={photoBackup.enabled}
          onChangeText={(destination) => onPhotoBackupChange({ destination })}
          placeholder="Фото с телефона"
          autoCapitalize="sentences"
        />
        {photoBackup.lastScanAt ? (
          <Text style={styles.muted}>
            Последняя проверка: {new Date(photoBackup.lastScanAt).toLocaleString()} · добавлено в
            очередь: {photoBackup.queuedCount}
          </Text>
        ) : null}
        {photoMessage ? <Notice tone={photoBackup.lastError ? 'red' : 'green'}>{photoMessage}</Notice> : null}
        <Button
          title="Проверить фото сейчас"
          onPress={onRunPhotoBackup}
          busy={photoBusy}
          disabled={!photoBackup.enabled || !photoBackup.spaceId}
        />
        <Button
          title="Пересканировать медиатеку"
          secondary
          onPress={() =>
            Alert.alert(
              'Сбросить индекс?',
              'Фотографии, уже загруженные на сервер, останутся там. Локальный индекс будет создан заново.',
              [
                { text: 'Отмена', style: 'cancel' },
                { text: 'Сбросить', style: 'destructive', onPress: onResetPhotoIndex },
              ],
            )
          }
        />
      </Card>

      <Notice tone="green">
        Пароль не хранится. Токен устройства и 24-часовая интернет-сессия лежат в защищённом
        системном хранилище телефона.
      </Notice>
      <Card>
        <Text style={styles.section}>Интернет-сессия</Text>
        <Text style={styles.muted}>
          Можно удалить только временную zrok-сессию и оставить телефон подтверждённым.
        </Text>
        <Button title="Заблокировать интернет-сессию" onPress={onLockInternet} secondary />
      </Card>
      <Card>
        <Text style={styles.section}>Отключение</Text>
        <Text style={styles.muted}>
          После удаления локального токена снова потребуются вход и подтверждение в Server Manager.
        </Text>
        <Button title="Забыть сервер на телефоне" onPress={onForget} danger />
      </Card>
    </Screen>
  );
}

const styles = StyleSheet.create({
  label: { color: colors.muted, fontWeight: '700', fontSize: 11, letterSpacing: 1, marginTop: spacing.xs },
  value: { color: colors.text, fontWeight: '700', fontSize: 17 },
  muted: { color: colors.muted, lineHeight: 20 },
  trusted: { color: colors.green, fontWeight: '800', marginTop: spacing.sm },
  section: { color: colors.text, fontWeight: '800', fontSize: 18 },
  switchRow: { flexDirection: 'row', alignItems: 'center', gap: spacing.md },
  switchText: { flex: 1, gap: 3 },
  switchLabel: { color: colors.text, fontWeight: '800', fontSize: 15 },
  disabled: { opacity: 0.5 },
  spaces: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm },
  spacePill: {
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surfaceAlt,
    borderRadius: 999,
    paddingVertical: 9,
    paddingHorizontal: 14,
  },
  spacePillActive: { backgroundColor: colors.red, borderColor: colors.red },
  spaceText: { color: colors.text, fontWeight: '700' },
});
