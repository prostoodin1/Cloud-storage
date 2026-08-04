import { StyleSheet, Text, View } from 'react-native';

import { Button, Card, Empty, Notice, Screen, Title } from '../components/Ui';
import { colors, radius, spacing } from '../theme';
import type { TransferRecord } from '../types';

function formatBytes(value: number): string {
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} КБ`;
  return `${(value / 1024 ** 2).toFixed(1)} МБ`;
}

const statusText: Record<TransferRecord['status'], string> = {
  queued: 'В очереди',
  running: 'Передача',
  paused: 'На паузе',
  completed: 'Готово',
  failed: 'Ошибка',
};

export function TransfersScreen({
  transfers,
  onRetry,
  onClearCompleted,
}: {
  transfers: TransferRecord[];
  onRetry: (id: string) => void;
  onClearCompleted: () => void;
}) {
  const active = transfers.filter((item) => item.status !== 'completed').length;
  return (
    <Screen>
      <Title subtitle={`${active} активных · очередь сохраняется после перезапуска`}>Передачи</Title>
      <Notice tone="blue">
        Загрузки используют серверную докачку частями. Скачанные файлы остаются в папке Cloud Storage и доступны офлайн.
      </Notice>
      {transfers.length === 0 ? <Empty>Передач пока нет.</Empty> : null}
      {transfers.map((transfer) => {
        const progress =
          transfer.totalBytes > 0
            ? Math.min(100, Math.round((transfer.completedBytes / transfer.totalBytes) * 100))
            : 0;
        return (
          <Card key={transfer.id}>
            <View style={styles.row}>
              <Text style={styles.direction}>{transfer.direction === 'upload' ? '↑' : '↓'}</Text>
              <View style={styles.body}>
                <Text style={styles.name} numberOfLines={2}>{transfer.name}</Text>
                <Text style={styles.meta}>
                  {statusText[transfer.status]} · {formatBytes(transfer.completedBytes)} / {formatBytes(transfer.totalBytes)}
                </Text>
              </View>
              <Text style={styles.percent}>{progress}%</Text>
            </View>
            <View style={styles.track}>
              <View style={[styles.fill, { width: `${progress}%` }]} />
            </View>
            {transfer.error ? <Text style={styles.error}>{transfer.error}</Text> : null}
            {transfer.status === 'failed' || transfer.status === 'paused' ? (
              <Button title="Повторить" onPress={() => onRetry(transfer.id)} secondary />
            ) : null}
          </Card>
        );
      })}
      {transfers.some((item) => item.status === 'completed') ? (
        <Button title="Убрать завершённые" onPress={onClearCompleted} secondary />
      ) : null}
    </Screen>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', alignItems: 'center', gap: spacing.sm },
  direction: { color: colors.red, fontSize: 28, fontWeight: '800', width: 30 },
  body: { flex: 1, gap: 4 },
  name: { color: colors.text, fontWeight: '700', fontSize: 16 },
  meta: { color: colors.muted, fontSize: 12 },
  percent: { color: colors.text, fontWeight: '800' },
  track: { height: 7, borderRadius: radius.sm, backgroundColor: colors.surfaceAlt, overflow: 'hidden' },
  fill: { height: '100%', backgroundColor: colors.red },
  error: { color: colors.red, lineHeight: 20 },
});
