export type DeviceStatus = 'pending' | 'trusted' | 'revoked' | 'offline';

export interface ConnectionProfile {
  serverUrl: string;
  serverName: string;
  username: string;
  deviceId: string;
  deviceName: string;
  deviceStatus: DeviceStatus;
}

export interface PhotoBackupSettings {
  enabled: boolean;
  wifiOnly: boolean;
  chargingOnly: boolean;
  includeVideos: boolean;
  destination: string;
  spaceId: string;
  scanOffset: number;
  initialScanComplete: boolean;
  queuedCount: number;
  lastScanAt: string;
  lastError?: string;
}

export interface DeviceRecord {
  id: string;
  user_id: string;
  username: string;
  user_display_name: string;
  name: string;
  platform: string;
  status: DeviceStatus;
}

export interface PairingResult {
  device: DeviceRecord;
  device_token: string;
  message: string;
}

export interface HealthResult {
  status: string;
  server_name?: string;
  version?: string;
  remote?: { enabled: boolean; public_url: string } | null;
  zrok?: { enabled: boolean; public_url?: string } | null;
}

export interface SpaceRecord {
  id: string;
  owner_user_id: string | null;
  name: string;
  kind: 'personal' | 'shared' | 'backup';
  quota_bytes: number;
  permission: 'read' | 'write' | 'owner' | null;
}

export interface FileEntry {
  id?: string;
  name: string;
  logical_path?: string;
  type: 'file' | 'directory';
  size_bytes?: number;
  sha256?: string;
  content_type?: string;
  version?: number;
  modified_at?: string;
}

export interface PublicShareResult {
  id: string;
  token: string;
  space_id: string;
  logical_path: string;
  kind: 'file' | 'directory';
  expires_at: string;
  url_path: string;
}

export interface MobileAdminOverview {
  summary: {
    users: number;
    trusted_devices: number;
    pending_devices: number;
    files: number;
    stored_bytes: number;
  };
  server_mode: { mode: 'normal' | 'read_only'; reason: string; changed_at: string };
  diagnostics: {
    status: 'healthy' | 'warning' | 'critical';
    active_warning_count: number;
    active_critical_count: number;
    latest_scan: Record<string, unknown> | null;
  };
  transfers: { inbound_active: number; outbound_active: number; failed: number };
  zrok: { enabled: boolean; state: string; public_url: string };
  users: Array<{
    id: string;
    username: string;
    display_name: string;
    role: 'admin' | 'member';
    quota_bytes: number;
    enabled: boolean;
    has_password: boolean;
  }>;
  devices: DeviceRecord[];
  storage: Array<{
    id: string;
    purpose: string;
    write_enabled: boolean;
    available: boolean;
    total_bytes: number;
    used_bytes: number;
    free_bytes: number;
    status: string;
  }>;
  backup_policies: Array<{
    target_root_id: string;
    enabled: boolean;
    interval_hours: number;
    next_run_at: string | null;
  }>;
  backups: Array<{
    id: string;
    status: string;
    target_root_id: string;
    created_at: string;
  }>;
  automation: { running: boolean; enabled: boolean; interval_seconds: number };
  control: {
    profile: string;
    interface_mode: 'simple' | 'detailed';
    security_mode: 'basic' | 'advanced';
    browser_access: 'all' | 'approved' | 'nobody';
    power: {
      source: string;
      percent: number | null;
      minutes_left: number | null;
      idle_seconds: number;
      sleep_armed: boolean;
    };
    report_schedules: number;
    sandbox_available: boolean;
    sandbox_runtime: string | null;
  };
}

export type MobileAdminAction =
  | 'device.approve'
  | 'device.revoke'
  | 'server.read_only'
  | 'server.normal'
  | 'user.create'
  | 'user.password'
  | 'user.enable'
  | 'user.disable'
  | 'diagnostics.quick'
  | 'diagnostics.full'
  | 'backup.run'
  | 'storage.write.pause'
  | 'storage.write.resume'
  | 'automation.pause'
  | 'automation.resume'
  | 'tunnel.restart'
  | 'core.restart';

export interface MobileCreateUserInput {
  username: string;
  displayName: string;
  quotaGiB: number;
  role: 'admin' | 'member';
  password: string;
  email: string;
}

export type TransferDirection = 'upload' | 'download';
export type TransferStatus = 'queued' | 'running' | 'paused' | 'completed' | 'failed';

export interface TransferRecord {
  id: string;
  direction: TransferDirection;
  status: TransferStatus;
  name: string;
  logicalPath: string;
  localUri: string;
  spaceId: string;
  totalBytes: number;
  completedBytes: number;
  remoteUploadId?: string;
  sourceAssetId?: string;
  error?: string;
  createdAt: string;
}

export interface PersistedState {
  connection: ConnectionProfile | null;
  transfers: TransferRecord[];
  photoBackup: PhotoBackupSettings;
}
