import { View } from 'react-native';
import { StatusBar } from 'expo-status-bar';

import { TabBar } from './components/TabBar';
import { FilesScreen } from './screens/FilesScreen';
import { ServerControlScreen } from './screens/ServerControlScreen';
import { colors } from './theme';
import type { MobileAdminOverview, SpaceRecord } from './types';

const spaces: SpaceRecord[] = [
  { id: 'personal', owner_user_id: 'owner', name: 'Мои файлы', kind: 'personal', quota_bytes: 500 * 1024 ** 3, permission: 'owner' },
  { id: 'family', owner_user_id: null, name: 'Семья', kind: 'shared', quota_bytes: 200 * 1024 ** 3, permission: 'write' },
];

const overview: MobileAdminOverview = {
  summary: { users: 4, trusted_devices: 7, pending_devices: 1, files: 1842, stored_bytes: 147 * 1024 ** 3 },
  server_mode: { mode: 'normal', reason: '', changed_at: '2026-08-04T10:00:00Z' },
  diagnostics: { status: 'healthy', active_warning_count: 0, active_critical_count: 0, latest_scan: null },
  transfers: { inbound_active: 2, outbound_active: 1, failed: 0 },
  zrok: { enabled: true, state: 'online', public_url: 'https://home-cloud.share.zrok.io' },
  users: [
    { id: 'owner', username: 'owner', display_name: 'Владелец', role: 'admin', quota_bytes: 500 * 1024 ** 3, enabled: true, has_password: true },
    { id: 'anna', username: 'anna', display_name: 'Анна', role: 'member', quota_bytes: 100 * 1024 ** 3, enabled: true, has_password: true },
  ],
  devices: [
    { id: 'phone', user_id: 'owner', username: 'owner', user_display_name: 'Владелец', name: 'iPhone 17 Pro', platform: 'iOS', status: 'trusted' },
    { id: 'pending', user_id: 'anna', username: 'anna', user_display_name: 'Анна', name: 'Pixel 11', platform: 'Android', status: 'pending' },
  ],
  storage: [
    { id: 'ssd-cache', purpose: 'primary', write_enabled: true, available: true, total_bytes: 2 * 1024 ** 4, used_bytes: 740 * 1024 ** 3, free_bytes: 1308 * 1024 ** 3, status: 'healthy' },
  ],
  backup_policies: [],
  backups: [],
  automation: { running: true, enabled: true, interval_seconds: 60 },
  control: {
    profile: 'recommended',
    interface_mode: 'simple',
    security_mode: 'advanced',
    browser_access: 'approved',
    power: {
      source: 'mains',
      percent: 100,
      minutes_left: null,
      idle_seconds: 42,
      sleep_armed: false,
    },
    report_schedules: 2,
    sandbox_available: true,
    sandbox_runtime: 'docker',
  },
};

const done = async () => undefined;

export function PreviewApp({ screen }: { screen: string }) {
  const server = screen === 'server';
  return (
    <View style={{ flex: 1, backgroundColor: colors.background }}>
      <StatusBar style="light" />
      {server ? (
        <ServerControlScreen
          overview={overview}
          loading={false}
          error=""
          onRefresh={() => undefined}
          onApprove={done}
          onRevoke={done}
          onSetMode={done}
          onCreateUser={done}
          onSetUserPassword={done}
          onSetUserEnabled={done}
          onRunDiagnostics={done}
          onRunBackup={done}
          onSetStorageWrite={done}
          onSetAutomation={done}
          onRestartTunnel={done}
          onRestartCore={done}
        />
      ) : (
        <FilesScreen
          spaces={spaces}
          selectedSpaceId="personal"
          directory=""
          entries={[
            { name: 'Фото с телефона', type: 'directory', modified_at: '2026-08-04T12:00:00Z' },
            { name: 'Документы', type: 'directory', modified_at: '2026-08-03T18:30:00Z' },
            { name: 'Проект.blend', type: 'file', size_bytes: 842_000_000, version: 4 },
            { name: 'Семейное видео.mp4', type: 'file', size_bytes: 2_740_000_000, version: 1 },
          ]}
          loading={false}
          error=""
          onSelectSpace={() => undefined}
          onOpenDirectory={() => undefined}
          onRefresh={() => undefined}
          onUpload={() => undefined}
          onDownload={() => undefined}
          onDelete={() => undefined}
          onCreateDirectory={() => undefined}
          onSearch={() => undefined}
          onShare={() => undefined}
        />
      )}
      <TabBar tab={server ? 'server' : 'files'} onChange={() => undefined} badge={2} admin />
    </View>
  );
}
