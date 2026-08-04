export type DeviceStatus = 'pending' | 'trusted' | 'revoked' | 'offline';

export interface ConnectionProfile {
  serverUrl: string;
  serverName: string;
  username: string;
  deviceId: string;
  deviceName: string;
  deviceStatus: DeviceStatus;
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
  type: 'file' | 'directory';
  size_bytes?: number;
  sha256?: string;
  content_type?: string;
  version?: number;
  modified_at?: string;
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
  error?: string;
  createdAt: string;
}

export interface PersistedState {
  connection: ConnectionProfile | null;
  transfers: TransferRecord[];
}
