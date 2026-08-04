import { StyleSheet, Text } from 'react-native';

import { Button, Card, Notice, Screen, Title } from '../components/Ui';
import { colors, spacing } from '../theme';
import type { ConnectionProfile } from '../types';

export function SettingsScreen({
  profile,
  onLockInternet,
  onForget,
}: {
  profile: ConnectionProfile;
  onLockInternet: () => void;
  onForget: () => void;
}) {
  return (
    <Screen>
      <Title subtitle="Подключение и безопасность">Настройки</Title>
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
      <Notice tone="green">
        Пароль не хранится. Токен устройства и 24-часовая интернет-сессия лежат в защищённом системном хранилище телефона.
      </Notice>
      <Card>
        <Text style={styles.section}>Интернет-сессия</Text>
        <Text style={styles.muted}>Можно удалить только временную zrok-сессию и оставить телефон подтверждённым.</Text>
        <Button title="Заблокировать интернет-сессию" onPress={onLockInternet} secondary />
      </Card>
      <Card>
        <Text style={styles.section}>Отключение</Text>
        <Text style={styles.muted}>После удаления локального токена снова потребуется вход и подтверждение в Server Manager.</Text>
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
});
