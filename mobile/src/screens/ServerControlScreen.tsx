import { Alert, Pressable, StyleSheet, Text, View } from 'react-native';

import { Button, Card, Empty, Notice, Screen, Title } from '../components/Ui';
import { colors, radius, spacing } from '../theme';
import type { MobileAdminOverview } from '../types';

function formatBytes(value: number): string {
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} МБ`;
  return `${(value / 1024 ** 3).toFixed(1)} ГБ`;
}

export function ServerControlScreen({
  overview,
  loading,
  error,
  onRefresh,
  onApprove,
  onRevoke,
  onSetMode,
}: {
  overview: MobileAdminOverview;
  loading: boolean;
  error: string;
  onRefresh: () => void;
  onApprove: (deviceId: string) => void;
  onRevoke: (deviceId: string) => void;
  onSetMode: (mode: 'normal' | 'read_only') => void;
}) {
  const pending = overview.devices.filter((item) => item.status === 'pending');
  const trusted = overview.devices.filter((item) => item.status === 'trusted');
  const mode = overview.server_mode.mode;
  return (
    <Screen>
      <Title subtitle="Доступно только подтверждённому администратору">Управление сервером</Title>
      {mode === 'read_only' ? (
        <Notice tone="red">Включён аварийный режим «только чтение»: новые записи и подключения заблокированы.</Notice>
      ) : (
        <Notice tone="green">Сервер работает в обычном режиме.</Notice>
      )}
      {error ? <Notice tone="red">{error}</Notice> : null}
      <View style={styles.metrics}>
        <Metric value={String(overview.summary.users)} label="Пользователи" />
        <Metric value={String(overview.summary.trusted_devices)} label="Устройства" />
        <Metric value={formatBytes(overview.summary.stored_bytes)} label="Занято" />
        <Metric
          value={overview.diagnostics.status === 'healthy' ? 'OK' : String(overview.diagnostics.active_critical_count + overview.diagnostics.active_warning_count)}
          label="Диагностика"
          alert={overview.diagnostics.status !== 'healthy'}
        />
      </View>
      <Card>
        <Text style={styles.section}>Защитный режим</Text>
        <Text style={styles.muted}>
          Опасное включение требует отдельного подтверждения. Возврат в обычный режим снова разрешает запись.
        </Text>
        {mode === 'normal' ? (
          <Button
            title="Включить только чтение"
            danger
            onPress={() =>
              Alert.alert(
                'Включить аварийный режим?',
                'Загрузки, удаления, новые подключения и фоновые задания будут остановлены.',
                [
                  { text: 'Отмена', style: 'cancel' },
                  { text: 'Включить', style: 'destructive', onPress: () => onSetMode('read_only') },
                ],
              )
            }
          />
        ) : (
          <Button title="Вернуть обычный режим" onPress={() => onSetMode('normal')} />
        )}
      </Card>
      <Text style={styles.section}>Ожидают подтверждения · {pending.length}</Text>
      {pending.length === 0 ? <Empty>Новых запросов нет.</Empty> : null}
      {pending.map((device) => (
        <Card key={device.id}>
          <Text style={styles.device}>{device.name}</Text>
          <Text style={styles.muted}>@{device.username} · {device.platform}</Text>
          <View style={styles.row}>
            <Pressable onPress={() => onApprove(device.id)} style={[styles.smallButton, styles.approve]}>
              <Text style={styles.smallText}>Подтвердить</Text>
            </Pressable>
            <Pressable onPress={() => onRevoke(device.id)} style={styles.smallButton}>
              <Text style={styles.smallText}>Отклонить</Text>
            </Pressable>
          </View>
        </Card>
      ))}
      <Text style={styles.section}>Пользователи · {overview.users.length}</Text>
      {overview.users.map((user) => (
        <Card key={user.id}>
          <Text style={styles.device}>{user.display_name}</Text>
          <Text style={styles.muted}>
            @{user.username} · {user.role === 'admin' ? 'Администратор' : 'Пользователь'} · {formatBytes(user.quota_bytes)}
          </Text>
        </Card>
      ))}
      <Text style={styles.section}>Подтверждённые устройства · {trusted.length}</Text>
      {trusted.map((device) => (
        <Card key={device.id}>
          <Text style={styles.device}>{device.name}</Text>
          <Text style={styles.muted}>@{device.username} · {device.platform}</Text>
          <Button
            title="Отключить устройство"
            secondary
            onPress={() =>
              Alert.alert('Отключить устройство?', 'Его токен будет отозван сразу.', [
                { text: 'Отмена', style: 'cancel' },
                { text: 'Отключить', style: 'destructive', onPress: () => onRevoke(device.id) },
              ])
            }
          />
        </Card>
      ))}
      <Button title="Обновить состояние" onPress={onRefresh} secondary busy={loading} />
    </Screen>
  );
}

function Metric({ value, label, alert = false }: { value: string; label: string; alert?: boolean }) {
  return (
    <View style={styles.metric}>
      <Text style={[styles.metricValue, alert && styles.alert]}>{value}</Text>
      <Text style={styles.metricLabel}>{label}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  metrics: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm },
  metric: {
    width: '48%',
    minHeight: 94,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surface,
    padding: spacing.md,
    justifyContent: 'center',
  },
  metricValue: { color: colors.text, fontSize: 24, fontWeight: '900' },
  metricLabel: { color: colors.muted, fontSize: 11, textTransform: 'uppercase' },
  alert: { color: colors.yellow },
  section: { color: colors.text, fontSize: 18, fontWeight: '800' },
  muted: { color: colors.muted, lineHeight: 20 },
  device: { color: colors.text, fontSize: 16, fontWeight: '800' },
  row: { flexDirection: 'row', gap: spacing.sm },
  smallButton: {
    flex: 1,
    minHeight: 44,
    alignItems: 'center',
    justifyContent: 'center',
    borderRadius: radius.sm,
    backgroundColor: colors.surfaceAlt,
    borderWidth: 1,
    borderColor: colors.border,
  },
  approve: { backgroundColor: colors.red, borderColor: colors.red },
  smallText: { color: colors.white, fontWeight: '800' },
});
