import { fetch } from 'expo/fetch';

import type {
  FileEntry,
  HealthResult,
  MobileAdminOverview,
  MobileAdminAction,
  MobileCreateUserInput,
  PairingResult,
  PublicShareResult,
  SpaceRecord,
} from './types';

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly retryAfterMs?: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }

  get needsInternetLogin(): boolean {
    return this.status === 403 && this.message.toLocaleLowerCase().includes('internet login');
  }
}

export function retryAfterMs(value: string | null): number | undefined {
  if (!value) return undefined;
  const delay = /^\d+$/.test(value.trim()) ? Number(value) * 1000 : Date.parse(value) - Date.now();
  return Number.isFinite(delay) ? Math.max(0, Math.min(3_600_000, delay)) : undefined;
}

export function normalizeServerUrl(value: string): string {
  const trimmed = value.trim().replace(/\/+$/, '');
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    throw new Error('Введите полный адрес сервера, например https://cloud.example.com');
  }
  const loopback = ['127.0.0.1', 'localhost', '::1'].includes(parsed.hostname);
  if (parsed.protocol !== 'https:' && !(loopback && parsed.protocol === 'http:')) {
    throw new Error('На телефоне разрешено только защищённое HTTPS-подключение.');
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error('Адрес сервера не должен содержать логин, пароль или параметры.');
  }
  return parsed.toString().replace(/\/$/, '');
}

function encodeLogicalPath(path: string): string {
  return path.split('/').map(encodeURIComponent).join('/');
}

export class CloudApi {
  readonly serverUrl: string;

  constructor(
    serverUrl: string,
    public deviceToken = '',
    public remoteSession = '',
  ) {
    this.serverUrl = normalizeServerUrl(serverUrl);
  }

  private authorizationHeaders(): Record<string, string> {
    const headers: Record<string, string> = {};
    if (this.deviceToken) {
      headers.Authorization = `Bearer ${this.deviceToken}`;
    }
    if (this.remoteSession) {
      headers['X-Cloud-Remote-Session'] = this.remoteSession;
    }
    return headers;
  }

  private async request<T>(
    route: string,
    init: RequestInit = {},
    authenticated = true,
  ): Promise<T> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20_000);
    try {
      const response = await fetch(`${this.serverUrl}${route}`, {
        ...init,
        headers: {
          Accept: 'application/json',
          ...(init.body ? { 'Content-Type': 'application/json' } : {}),
          ...(authenticated ? this.authorizationHeaders() : {}),
          ...(init.headers ?? {}),
        },
        signal: controller.signal,
      });
      const text = await response.text();
      const payload = text ? (JSON.parse(text) as T | { detail?: string }) : ({} as T);
      if (!response.ok) {
        const detail =
          typeof payload === 'object' && payload && 'detail' in payload
            ? String(payload.detail ?? `Ошибка сервера ${response.status}`)
            : `Ошибка сервера ${response.status}`;
        throw new ApiError(response.status, detail, retryAfterMs(response.headers.get('Retry-After')));
      }
      return payload as T;
    } catch (error) {
      if (error instanceof ApiError) {
        throw error;
      }
      if (error instanceof Error && error.name === 'AbortError') {
        throw new TypeError('Сервер не ответил за 20 секунд.');
      }
      throw new TypeError('Не удалось подключиться к серверу. Проверьте адрес, HTTPS и интернет.');
    } finally {
      clearTimeout(timeout);
    }
  }

  health(): Promise<HealthResult> {
    return this.request<HealthResult>('/v1/health', {}, false);
  }

  loginNewDevice(
    username: string,
    password: string,
    deviceName: string,
    platform: string,
  ): Promise<PairingResult> {
    return this.request<PairingResult>(
      '/v1/auth/device-login',
      {
        method: 'POST',
        body: JSON.stringify({
          username,
          password,
          device_name: deviceName,
          platform,
        }),
      },
      false,
    );
  }

  redeemInvitation(
    code: string,
    password: string | null,
    deviceName: string,
    platform: string,
  ): Promise<PairingResult> {
    return this.request<PairingResult>(
      '/v1/pairing/redeem',
      {
        method: 'POST',
        body: JSON.stringify({
          code,
          ...(password ? { password } : {}),
          device_name: deviceName,
          platform,
        }),
      },
      false,
    );
  }

  pairingStatus(): Promise<{ device_id: string; status: string }> {
    return this.request('/v1/pairing/status');
  }

  createRemoteSession(
    username: string,
    password: string,
  ): Promise<{ session_token: string; expires_at: number; username: string }> {
    return this.request('/v1/remote/session', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
  }

  listSpaces(): Promise<SpaceRecord[]> {
    return this.request('/v1/spaces');
  }

  listEntries(spaceId: string, directory: string): Promise<FileEntry[]> {
    return this.request(
      `/v1/spaces/${encodeURIComponent(spaceId)}/entries?directory=${encodeURIComponent(directory)}`,
    );
  }

  searchEntries(spaceId: string, query: string, directory = ''): Promise<FileEntry[]> {
    return this.request(
      `/v1/spaces/${encodeURIComponent(spaceId)}/search?query=${encodeURIComponent(query)}&directory=${encodeURIComponent(directory)}`,
    );
  }

  createPublicShare(
    spaceId: string,
    logicalPath: string,
    kind: FileEntry['type'],
    ttlHours = 24,
  ): Promise<PublicShareResult> {
    return this.request('/v1/shares', {
      method: 'POST',
      body: JSON.stringify({
        space_id: spaceId,
        logical_path: logicalPath,
        kind,
        ttl_hours: ttlHours,
      }),
    });
  }

  createDirectory(spaceId: string, logicalPath: string): Promise<Record<string, unknown>> {
    return this.request(`/v1/spaces/${encodeURIComponent(spaceId)}/directories`, {
      method: 'POST',
      body: JSON.stringify({ logical_path: logicalPath }),
    });
  }

  deleteEntry(spaceId: string, logicalPath: string, type: FileEntry['type']): Promise<void> {
    const kind = type === 'directory' ? 'directories' : 'files';
    return this.request(
      `/v1/spaces/${encodeURIComponent(spaceId)}/${kind}/${encodeLogicalPath(logicalPath)}`,
      { method: 'DELETE' },
    );
  }

  createUpload(
    spaceId: string,
    logicalPath: string,
    sizeBytes: number,
    contentType: string,
  ): Promise<{ id: string; received_bytes: number; status: string; max_chunk_bytes?: number }> {
    return this.request(`/v1/spaces/${encodeURIComponent(spaceId)}/uploads`, {
      method: 'POST',
      body: JSON.stringify({
        logical_path: logicalPath,
        size_bytes: sizeBytes,
        content_type: contentType,
      }),
    });
  }

  uploadStatus(uploadId: string): Promise<{ id: string; received_bytes: number; status: string; max_chunk_bytes?: number }> {
    return this.request(`/v1/uploads/${encodeURIComponent(uploadId)}`);
  }

  async uploadChunk(uploadId: string, offset: number, chunk: Blob): Promise<number> {
    const controller = new AbortController();
    const budget = Math.max(60_000, Math.min(600_000, chunk.size / (32 * 1024) * 1000 + 30_000));
    const timer = setTimeout(() => controller.abort(), budget);
    try {
      const response = await fetch(`${this.serverUrl}/v1/uploads/${encodeURIComponent(uploadId)}`, {
        method: 'PATCH', signal: controller.signal,
        headers: { ...this.authorizationHeaders(), Accept: 'application/json',
          'Content-Type': 'application/offset+octet-stream', 'Upload-Offset': String(offset),
          'Content-Length': String(chunk.size) }, body: chunk,
      });
      const text = await response.text();
      let payload: { received_bytes?: number; detail?: string } = {};
      try { payload = text ? JSON.parse(text) : {}; } catch {
        if (response.ok) throw new TypeError('Сервер вернул неверное подтверждение загрузки.');
      }
      if (!response.ok) throw new ApiError(response.status, payload.detail ?? `Ошибка загрузки ${response.status}`, retryAfterMs(response.headers.get('Retry-After')));
      return Number(payload.received_bytes ?? offset + chunk.size);
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new TypeError('Соединение прервано при загрузке; передача будет продолжена.');
    } finally { clearTimeout(timer); }
  }

  completeUpload(uploadId: string): Promise<Record<string, unknown>> {
    return this.request(`/v1/uploads/${encodeURIComponent(uploadId)}/complete`, {
      method: 'POST',
    });
  }

  fileUrl(spaceId: string, logicalPath: string): string {
    return `${this.serverUrl}/v1/spaces/${encodeURIComponent(spaceId)}/files/${encodeLogicalPath(logicalPath)}`;
  }

  fileHeaders(): Record<string, string> {
    return this.authorizationHeaders();
  }

  async downloadRange(spaceId: string, logicalPath: string, offset: number, length: number, sha256: string) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30_000);
    try {
      return await fetch(this.fileUrl(spaceId, logicalPath), {
        headers: { ...this.fileHeaders(), Range: `bytes=${offset}-${offset + length - 1}`,
          ...(sha256 ? { 'If-Range': `"sha256:${sha256}"` } : {}) },
        signal: controller.signal,
      });
    } catch {
      throw new TypeError('Соединение прервано при скачивании.');
    } finally { clearTimeout(timer); }
  }

  mobileAdminOverview(): Promise<MobileAdminOverview> {
    return this.request('/v1/mobile/admin/overview');
  }

  mobileConfirmAdminAction(
    password: string,
    action: MobileAdminAction,
  ): Promise<{ confirmation_token: string; action: string; expires_at: number }> {
    return this.request('/v1/mobile/admin/confirm', {
      method: 'POST',
      body: JSON.stringify({ password, action }),
    });
  }

  mobileSetDeviceStatus(
    deviceId: string,
    status: 'approve' | 'revoke',
    confirmationToken: string,
  ): Promise<void> {
    return this.request(
      `/v1/mobile/admin/devices/${encodeURIComponent(deviceId)}/${status}`,
      { method: 'POST', headers: { 'X-Cloud-Admin-Confirmation': confirmationToken } },
    );
  }

  mobileSetServerMode(
    mode: 'normal' | 'read_only',
    reason: string,
    confirmationToken: string,
  ): Promise<Record<string, unknown>> {
    return this.request('/v1/mobile/admin/server-mode', {
      method: 'PUT',
      body: JSON.stringify({ mode, reason, confirmed: mode === 'read_only' }),
      headers: { 'X-Cloud-Admin-Confirmation': confirmationToken },
    });
  }

  mobileCreateUser(
    input: MobileCreateUserInput,
    confirmationToken: string,
  ): Promise<Record<string, unknown>> {
    return this.request('/v1/mobile/admin/users', {
      method: 'POST',
      body: JSON.stringify({
        username: input.username,
        display_name: input.displayName,
        quota_gib: input.quotaGiB,
        role: input.role,
        password: input.password,
        email: input.email,
        prepare_access: true,
      }),
      headers: { 'X-Cloud-Admin-Confirmation': confirmationToken },
    });
  }

  mobileSetUserPassword(
    userId: string,
    password: string,
    confirmationToken: string,
  ): Promise<Record<string, unknown>> {
    return this.request(`/v1/mobile/admin/users/${encodeURIComponent(userId)}/password`, {
      method: 'PUT',
      body: JSON.stringify({ password }),
      headers: { 'X-Cloud-Admin-Confirmation': confirmationToken },
    });
  }

  mobileSetUserEnabled(
    userId: string,
    enabled: boolean,
    confirmationToken: string,
  ): Promise<Record<string, unknown>> {
    return this.request(`/v1/mobile/admin/users/${encodeURIComponent(userId)}/enabled`, {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
      headers: { 'X-Cloud-Admin-Confirmation': confirmationToken },
    });
  }

  mobileRunDiagnostics(
    kind: 'quick' | 'full',
    confirmationToken: string,
  ): Promise<Record<string, unknown>> {
    return this.request('/v1/mobile/admin/diagnostics/scans', {
      method: 'POST',
      body: JSON.stringify({ kind }),
      headers: { 'X-Cloud-Admin-Confirmation': confirmationToken },
    });
  }

  mobileRunBackup(targetRootId: string, confirmationToken: string): Promise<Record<string, unknown>> {
    return this.request(`/v1/mobile/admin/backups/${encodeURIComponent(targetRootId)}/run`, {
      method: 'POST',
      headers: { 'X-Cloud-Admin-Confirmation': confirmationToken },
    });
  }

  mobileSetStorageWrite(
    rootId: string,
    enabled: boolean,
    confirmationToken: string,
  ): Promise<Record<string, unknown>> {
    return this.request(`/v1/mobile/admin/storage/${encodeURIComponent(rootId)}/write`, {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
      headers: { 'X-Cloud-Admin-Confirmation': confirmationToken },
    });
  }

  mobileSetAutomation(enabled: boolean, confirmationToken: string): Promise<Record<string, unknown>> {
    return this.request('/v1/mobile/admin/automation/settings', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
      headers: { 'X-Cloud-Admin-Confirmation': confirmationToken },
    });
  }

  mobileRestartTunnel(providerId: string, confirmationToken: string): Promise<Record<string, unknown>> {
    return this.request(`/v1/mobile/admin/tunnels/${encodeURIComponent(providerId)}/restart`, {
      method: 'POST',
      headers: { 'X-Cloud-Admin-Confirmation': confirmationToken },
    });
  }

  mobileRestartCore(confirmationToken: string): Promise<Record<string, unknown>> {
    return this.request('/v1/mobile/admin/core/restart', {
      method: 'POST',
      headers: { 'X-Cloud-Admin-Confirmation': confirmationToken },
    });
  }
}
