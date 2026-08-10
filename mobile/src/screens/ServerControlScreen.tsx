import { useState } from 'react';
import { Alert, Pressable, StyleSheet, Text, View } from 'react-native';

import { Button, Card, Empty, Field, Notice, Screen, Title } from '../components/Ui';
import { colors, radius, spacing } from '../theme';
import type { MobileAdminOverview, MobileCreateUserInput } from '../types';

function formatBytes(value: number): string {
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} МБ`;
  return `${(value / 1024 ** 3).toFixed(1)} ГБ`;
}

interface Confirmation {
  title: string;
  description: string;
  run: (adminPassword: string) => Promise<void>;
}

export function ServerControlScreen({
  overview,
  loading,
  error,
  onRefresh,
  onApprove,
  onRevoke,
  onSetMode,
  onCreateUser,
  onSetUserPassword,
  onSetUserEnabled,
  onRunDiagnostics,
  onRunBackup,
  onSetStorageWrite,
  onSetAutomation,
  onRestartTunnel,
  onRestartCore,
}: {
  overview: MobileAdminOverview;
  loading: boolean;
  error: string;
  onRefresh: () => void;
  onApprove: (deviceId: string, adminPassword: string) => Promise<void>;
  onRevoke: (deviceId: string, adminPassword: string) => Promise<void>;
  onSetMode: (mode: 'normal' | 'read_only', adminPassword: string) => Promise<void>;
  onCreateUser: (input: MobileCreateUserInput, adminPassword: string) => Promise<void>;
  onSetUserPassword: (userId: string, password: string, adminPassword: string) => Promise<void>;
  onSetUserEnabled: (userId: string, enabled: boolean, adminPassword: string) => Promise<void>;
  onRunDiagnostics: (kind: 'quick' | 'full', adminPassword: string) => Promise<void>;
  onRunBackup: (rootId: string, adminPassword: string) => Promise<void>;
  onSetStorageWrite: (rootId: string, enabled: boolean, adminPassword: string) => Promise<void>;
  onSetAutomation: (enabled: boolean, adminPassword: string) => Promise<void>;
  onRestartTunnel: (adminPassword: string) => Promise<void>;
  onRestartCore: (adminPassword: string) => Promise<void>;
}) {
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [adminPassword, setAdminPassword] = useState('');
  const [confirming, setConfirming] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [username, setUsername] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [email, setEmail] = useState('');
  const [quota, setQuota] = useState('100');
  const [role, setRole] = useState<'admin' | 'member'>('member');
  const [userPassword, setUserPassword] = useState('');
  const [resetUserId, setResetUserId] = useState('');
  const [resetPassword, setResetPassword] = useState('');

  const pending = overview.devices.filter((item) => item.status === 'pending');
  const trusted = overview.devices.filter((item) => item.status === 'trusted');
  const mode = overview.server_mode.mode;

  const requestConfirmation = (
    title: string,
    description: string,
    run: (password: string) => Promise<void>,
  ) => {
    setAdminPassword('');
    setConfirmation({ title, description, run });
  };

  const confirm = async () => {
    if (!confirmation || !adminPassword) return;
    setConfirming(true);
    try {
      await confirmation.run(adminPassword);
      setConfirmation(null);
      setAdminPassword('');
    } catch {
      // The error from the API is shown above the page.
    } finally {
      setConfirming(false);
    }
  };

  const submitNewUser = () => {
    const quotaGiB = Number.parseInt(quota, 10);
    if (!username.trim() || !displayName.trim() || !userPassword || !Number.isFinite(quotaGiB)) {
      Alert.alert('Заполните форму', 'Нужны логин, имя, квота и временный пароль.');
      return;
    }
    const input: MobileCreateUserInput = {
      username: username.trim(),
      displayName: displayName.trim(),
      quotaGiB,
      role,
      password: userPassword,
      email: email.trim(),
    };
    requestConfirmation(
      'Создать пользователя?',
      `Будет создана учётная запись @${input.username} и личное хранилище ${input.quotaGiB} ГБ.`,
      async (password) => {
        await onCreateUser(input, password);
        setUsername('');
        setDisplayName('');
        setEmail('');
        setQuota('100');
        setRole('member');
        setUserPassword('');
        setFormOpen(false);
      },
    );
  };

  return (
    <Screen>
      <Title subtitle="Безопасное управление с подтверждённого телефона">
        Управление сервером
      </Title>
      {mode === 'read_only' ? (
        <Notice tone="red">Включён аварийный режим «только чтение».</Notice>
      ) : (
        <Notice tone="green">Сервер работает в обычном режиме.</Notice>
      )}
      {error ? <Notice tone="red">{error}</Notice> : null}
      {confirmation ? (
        <Card>
          <Text style={styles.section}>{confirmation.title}</Text>
          <Text style={styles.muted}>{confirmation.description}</Text>
          <Notice tone="yellow">
            Введите пароль администратора. Если на телефоне настроены Face ID, Touch ID или
            отпечаток, приложение запросит и локальное подтверждение.
          </Notice>
          <Field
            label="Пароль администратора"
            value={adminPassword}
            onChangeText={setAdminPassword}
            secureTextEntry
            autoComplete="current-password"
          />
          <Button title="Подтвердить действие" onPress={() => void confirm()} busy={confirming} disabled={!adminPassword} />
          <Button title="Отмена" secondary onPress={() => setConfirmation(null)} />
        </Card>
      ) : null}

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
        <Text style={styles.section}>Idea 4 · система</Text>
        <Text style={styles.muted}>
          Профиль: {overview.control.profile} · защита: {overview.control.security_mode} ·
          браузер: {overview.control.browser_access}
        </Text>
        <Text style={styles.muted}>
          Питание: {overview.control.power.source} · заряд{' '}
          {overview.control.power.percent ?? '—'}% · осталось{' '}
          {overview.control.power.minutes_left ?? '—'} мин · сон{' '}
          {overview.control.power.sleep_armed ? 'разрешён' : 'не активирован'}
        </Text>
        <Text style={styles.muted}>
          Отчётов: {overview.control.report_schedules} · ячейки:{' '}
          {overview.control.sandbox_available
            ? overview.control.sandbox_runtime
            : 'Docker/Podman не найден'}
        </Text>
      </Card>

      <Card>
        <Text style={styles.section}>Обслуживание</Text>
        <Text style={styles.muted}>
          Диагностика, автоматика и перезапуски выполняются сервером; приложение показывает
          актуальное состояние после запуска.
        </Text>
        <Button
          title="Быстрая диагностика"
          secondary
          onPress={() => requestConfirmation('Запустить диагностику?', 'Сервер проверит основные службы и накопители.', (password) => onRunDiagnostics('quick', password))}
        />
        <Button
          title="Полная диагностика"
          secondary
          onPress={() => requestConfirmation('Запустить полную диагностику?', 'Проверка займёт больше времени и прочитает данные хранилищ.', (password) => onRunDiagnostics('full', password))}
        />
        <Button
          title={overview.automation.enabled ? 'Приостановить автоматизацию' : 'Возобновить автоматизацию'}
          secondary
          onPress={() => requestConfirmation(
            overview.automation.enabled ? 'Приостановить автоматизацию?' : 'Возобновить автоматизацию?',
            `Интервал проверок: ${overview.automation.interval_seconds} сек.`,
            (password) => onSetAutomation(!overview.automation.enabled, password),
          )}
        />
        <Button
          title="Перезапустить zrok"
          secondary
          onPress={() => requestConfirmation('Перезапустить интернет-туннель?', 'Текущие удалённые подключения могут кратковременно прерваться.', onRestartTunnel)}
        />
        <Button
          title="Перезапустить Core"
          danger
          onPress={() => requestConfirmation('Перезапустить серверное ядро?', 'Передачи будут поставлены на восстановление после запуска службы.', onRestartCore)}
        />
      </Card>

      <Text style={styles.section}>Накопители · {overview.storage.length}</Text>
      {overview.storage.length === 0 ? <Empty>Настроенных накопителей нет.</Empty> : null}
      {overview.storage.map((root) => (
        <Card key={root.id}>
          <View style={styles.headingRow}>
            <View style={styles.flex}>
              <Text style={styles.device}>{root.id}</Text>
              <Text style={styles.muted}>
                {root.purpose} · свободно {formatBytes(root.free_bytes)} из {formatBytes(root.total_bytes)}
              </Text>
            </View>
            <Text style={root.available && root.write_enabled ? styles.enabled : styles.disabledText}>
              {!root.available ? 'Недоступен' : root.write_enabled ? 'Запись разрешена' : 'Запись на паузе'}
            </Text>
          </View>
          {root.available ? (
            <Button
              title={root.write_enabled ? 'Приостановить новые записи' : 'Возобновить записи'}
              secondary
              onPress={() => requestConfirmation(
                root.write_enabled ? 'Приостановить запись?' : 'Возобновить запись?',
                root.write_enabled
                  ? 'Текущие чтения останутся доступны, новые файлы не будут размещаться на этом накопителе.'
                  : 'Накопитель снова сможет принимать новые файлы.',
                (password) => onSetStorageWrite(root.id, !root.write_enabled, password),
              )}
            />
          ) : null}
        </Card>
      ))}

      <Text style={styles.section}>Резервные копии</Text>
      {overview.backup_policies.length === 0 ? <Empty>Политики резервного копирования настраиваются в Server Manager.</Empty> : null}
      {overview.backup_policies.map((policy) => (
        <Card key={policy.target_root_id}>
          <Text style={styles.device}>Копия на {policy.target_root_id}</Text>
          <Text style={styles.muted}>
            {policy.enabled ? `Автоматически каждые ${policy.interval_hours} ч.` : 'Автоматика отключена'}
          </Text>
          <Button
            title="Запустить копирование сейчас"
            secondary
            onPress={() => requestConfirmation(
              'Запустить резервную копию?',
              `Снимок будет создан на ${policy.target_root_id}.`,
              (password) => onRunBackup(policy.target_root_id, password),
            )}
          />
        </Card>
      ))}

      <Card>
        <Text style={styles.section}>Защитный режим</Text>
        <Text style={styles.muted}>Переключение режима требует отдельного пароля и одноразового подтверждения.</Text>
        {mode === 'normal' ? (
          <Button
            title="Включить только чтение"
            danger
            onPress={() =>
              requestConfirmation(
                'Включить только чтение?',
                'Загрузки, удаления и новые подключения будут остановлены.',
                (password) => onSetMode('read_only', password),
              )
            }
          />
        ) : (
          <Button
            title="Вернуть обычный режим"
            onPress={() =>
              requestConfirmation(
                'Вернуть обычный режим?',
                'Сервер снова разрешит запись.',
                (password) => onSetMode('normal', password),
              )
            }
          />
        )}
      </Card>

      <Text style={styles.section}>Ожидают подтверждения · {pending.length}</Text>
      {pending.length === 0 ? <Empty>Новых запросов нет.</Empty> : null}
      {pending.map((device) => (
        <Card key={device.id}>
          <Text style={styles.device}>{device.name}</Text>
          <Text style={styles.muted}>@{device.username} · {device.platform}</Text>
          <View style={styles.row}>
            <Pressable
              onPress={() =>
                requestConfirmation(
                  'Подтвердить устройство?',
                  `${device.name} получит доступ пользователя @${device.username}.`,
                  (password) => onApprove(device.id, password),
                )
              }
              style={[styles.smallButton, styles.approve]}
            >
              <Text style={styles.smallText}>Подтвердить</Text>
            </Pressable>
            <Pressable
              onPress={() =>
                requestConfirmation(
                  'Отклонить устройство?',
                  `${device.name} не получит доступ.`,
                  (password) => onRevoke(device.id, password),
                )
              }
              style={styles.smallButton}
            >
              <Text style={styles.smallText}>Отклонить</Text>
            </Pressable>
          </View>
        </Card>
      ))}

      <View style={styles.headingRow}>
        <Text style={styles.section}>Пользователи · {overview.users.length}</Text>
        <Pressable onPress={() => setFormOpen((value) => !value)}>
          <Text style={styles.link}>{formOpen ? 'Закрыть' : '+ Создать'}</Text>
        </Pressable>
      </View>
      {formOpen ? (
        <Card>
          <Text style={styles.section}>Новый пользователь</Text>
          <Field label="Логин" value={username} onChangeText={setUsername} placeholder="ivan" />
          <Field label="Имя" value={displayName} onChangeText={setDisplayName} placeholder="Иван" autoCapitalize="words" />
          <Field label="Email для файла входа" value={email} onChangeText={setEmail} placeholder="user@example.com" autoCapitalize="none" keyboardType="email-address" />
          <Field label="Квота, ГБ" value={quota} onChangeText={setQuota} keyboardType="number-pad" />
          <Field label="Временный пароль" value={userPassword} onChangeText={setUserPassword} secureTextEntry />
          <View style={styles.row}>
            {(['member', 'admin'] as const).map((item) => (
              <Pressable key={item} onPress={() => setRole(item)} style={[styles.role, role === item && styles.roleActive]}>
                <Text style={styles.smallText}>{item === 'admin' ? 'Администратор' : 'Пользователь'}</Text>
              </Pressable>
            ))}
          </View>
          <Button title="Создать личное хранилище" onPress={submitNewUser} />
        </Card>
      ) : null}

      {overview.users.map((user) => (
        <Card key={user.id}>
          <View style={styles.headingRow}>
            <View style={styles.flex}>
              <Text style={styles.device}>{user.display_name}</Text>
              <Text style={styles.muted}>
                @{user.username} · {user.role === 'admin' ? 'Администратор' : 'Пользователь'} · {formatBytes(user.quota_bytes)}
              </Text>
            </View>
            <Text style={user.enabled ? styles.enabled : styles.disabledText}>{user.enabled ? 'Включён' : 'Отключён'}</Text>
          </View>
          {resetUserId === user.id ? (
            <>
              <Field label="Новый пароль" value={resetPassword} onChangeText={setResetPassword} secureTextEntry />
              <Button
                title="Сохранить новый пароль"
                disabled={!resetPassword}
                onPress={() => {
                  const nextPassword = resetPassword;
                  requestConfirmation(
                    'Сменить пароль?',
                    `Пароль @${user.username} будет изменён, активные интернет-сессии станут недействительны.`,
                    async (password) => {
                      await onSetUserPassword(user.id, nextPassword, password);
                      setResetUserId('');
                      setResetPassword('');
                    },
                  );
                }}
              />
              <Button title="Отмена" secondary onPress={() => { setResetUserId(''); setResetPassword(''); }} />
            </>
          ) : (
            <View style={styles.row}>
              <Pressable onPress={() => setResetUserId(user.id)} style={styles.smallButton}>
                <Text style={styles.smallText}>Сменить пароль</Text>
              </Pressable>
              <Pressable
                onPress={() =>
                  requestConfirmation(
                    user.enabled ? 'Отключить пользователя?' : 'Включить пользователя?',
                    user.enabled
                      ? `Все устройства @${user.username} будут отозваны.`
                      : `Учётная запись @${user.username} снова сможет входить.`,
                    (password) => onSetUserEnabled(user.id, !user.enabled, password),
                  )
                }
                style={[styles.smallButton, user.enabled && styles.dangerOutline]}
              >
                <Text style={styles.smallText}>{user.enabled ? 'Отключить' : 'Включить'}</Text>
              </Pressable>
            </View>
          )}
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
              requestConfirmation(
                'Отключить устройство?',
                'Его токен будет отозван сразу.',
                (password) => onRevoke(device.id, password),
              )
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
  metric: { width: '48%', minHeight: 94, borderRadius: radius.md, borderWidth: 1, borderColor: colors.border, backgroundColor: colors.surface, padding: spacing.md, justifyContent: 'center' },
  metricValue: { color: colors.text, fontSize: 24, fontWeight: '900' },
  metricLabel: { color: colors.muted, fontSize: 11, textTransform: 'uppercase' },
  alert: { color: colors.yellow },
  section: { color: colors.text, fontSize: 18, fontWeight: '800' },
  muted: { color: colors.muted, lineHeight: 20 },
  device: { color: colors.text, fontSize: 16, fontWeight: '800' },
  row: { flexDirection: 'row', gap: spacing.sm },
  headingRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: spacing.sm },
  flex: { flex: 1 },
  link: { color: colors.red, fontWeight: '800' },
  enabled: { color: colors.green, fontWeight: '800', fontSize: 12 },
  disabledText: { color: colors.red, fontWeight: '800', fontSize: 12 },
  smallButton: { flex: 1, minHeight: 44, alignItems: 'center', justifyContent: 'center', borderRadius: radius.sm, backgroundColor: colors.surfaceAlt, borderWidth: 1, borderColor: colors.border, paddingHorizontal: spacing.sm },
  approve: { backgroundColor: colors.red, borderColor: colors.red },
  dangerOutline: { borderColor: colors.redDark },
  smallText: { color: colors.white, fontWeight: '800', textAlign: 'center' },
  role: { flex: 1, minHeight: 44, alignItems: 'center', justifyContent: 'center', borderRadius: radius.sm, borderWidth: 1, borderColor: colors.border },
  roleActive: { backgroundColor: colors.red, borderColor: colors.red },
});
