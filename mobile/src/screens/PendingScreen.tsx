import { StyleSheet, Text } from 'react-native';

import { Brand, Button, Card, Notice, Screen, Title } from '../components/Ui';
import { colors, spacing } from '../theme';
import type { ConnectionProfile } from '../types';

export function PendingScreen({
  profile,
  checking,
  error,
  onCheck,
  onForget,
}: {
  profile: ConnectionProfile;
  checking: boolean;
  error: string;
  onCheck: () => void;
  onForget: () => void;
}) {
  return (
    <Screen>
      <Brand />
      <Title subtitle={profile.serverName}>Ожидается подтверждение</Title>
      <Notice tone="yellow">
        Запрос уже отправлен. Откройте Server Manager → Настройки → Подключение устройств и подтвердите «{profile.deviceName}».
      </Notice>
      <Card>
        <Text style={styles.label}>АККАУНТ</Text>
        <Text style={styles.value}>@{profile.username}</Text>
        <Text style={styles.label}>СЕРВЕР</Text>
        <Text style={styles.value}>{profile.serverUrl}</Text>
        <Text style={styles.label}>СТАТУС</Text>
        <Text style={styles.pending}>Ожидает администратора</Text>
        {error ? <Notice tone="red">{error}</Notice> : null}
        <Button title="Проверить подтверждение" onPress={onCheck} busy={checking} />
        <Button title="Забыть подключение" onPress={onForget} secondary />
      </Card>
      <Text style={styles.hint}>Приложение также проверяет статус автоматически каждые 15 секунд.</Text>
    </Screen>
  );
}

const styles = StyleSheet.create({
  label: { color: colors.muted, fontWeight: '700', fontSize: 11, letterSpacing: 1 },
  value: { color: colors.text, fontSize: 16, marginBottom: spacing.sm },
  pending: { color: colors.yellow, fontSize: 16, fontWeight: '800' },
  hint: { color: colors.muted, textAlign: 'center', lineHeight: 20 },
});
