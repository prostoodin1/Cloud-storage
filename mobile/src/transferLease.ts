import { openDatabaseSync } from 'expo-sqlite';
import type { TransferRecord } from './types';

const TTL = 60_000;
let database: ReturnType<typeof openDatabaseSync> | undefined;
const registered = new Set<string>();

function db() {
  if (!database) {
    const opened = openDatabaseSync('cloud-storage-transfer-leases.db');
    try { opened.execSync('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=1000; CREATE TABLE IF NOT EXISTS transfer_leases (job TEXT PRIMARY KEY, owner TEXT NOT NULL, expires INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS transfer_checkpoints (server TEXT NOT NULL, id TEXT NOT NULL, record TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(server,id))'); }
    catch (error) { opened.closeSync(); throw error; }
    database = opened;
  }
  return database;
}

// JSON remains a profile/settings backup. Queue rows are merged rather than
// replacing another runtime's newer progress with a stale whole-state snapshot.
export function registerTransferRecords(server: string, records: TransferRecord[]): void {
  const fresh = records.filter(record => !registered.has(server + '\n' + record.id));
  if (!fresh.length) return;
  const database = db();
  database.execSync('BEGIN IMMEDIATE');
  try {
    for (const record of fresh) database.runSync('INSERT OR IGNORE INTO transfer_checkpoints(server,id,record) VALUES (?,?,?)', server, record.id, JSON.stringify(record));
    database.execSync('COMMIT');
    for (const record of fresh) registered.add(server + '\n' + record.id);
  } catch (error) { database.execSync('ROLLBACK'); throw error; }
}

export function mergeTransferRecords(server: string, records: TransferRecord[]): TransferRecord[] {
  registerTransferRecords(server, records);
  const result = new Map(records.map(record => [record.id, record]));
  for (const row of db().getAllSync<{ id: string; record: string; deleted: number }>('SELECT id,record,deleted FROM transfer_checkpoints WHERE server=?', server)) {
    registered.add(server + '\n' + row.id);
    if (row.deleted) result.delete(row.id);
    else result.set(row.id, JSON.parse(row.record) as TransferRecord);
  }
  return [...result.values()];
}

export function resetTransferRecord(server: string, record: TransferRecord): void {
  const database = db();
  database.execSync('BEGIN IMMEDIATE');
  try {
    database.runSync('DELETE FROM transfer_leases WHERE job=?', server + '\n' + record.id);
    database.runSync('INSERT INTO transfer_checkpoints(server,id,record) VALUES (?,?,?) ON CONFLICT(server,id) DO UPDATE SET record=excluded.record,deleted=0', server, record.id, JSON.stringify(record));
    database.execSync('COMMIT');
  } catch (error) { database.execSync('ROLLBACK'); throw error; }
}

export function clearCompletedRecords(server: string): void {
  for (const record of mergeTransferRecords(server, [])) {
    if (record.status === 'completed') db().runSync('UPDATE transfer_checkpoints SET deleted=1 WHERE server=? AND id=? AND json_extract(record,\'$.status\')=\'completed\'', server, record.id);
  }
}

export class TransferLeaseLost extends Error {}

export function claimTransfer(server: string, id: string) {
  if (db().getFirstSync<{ deleted: number }>('SELECT deleted FROM transfer_checkpoints WHERE server=? AND id=?',server,id)?.deleted) return null;
  const job = server + '\n' + id;
  const owner = Date.now().toString(36) + Math.random().toString(36);
  const now = Date.now();
  const claimed = db().runSync(
    'INSERT INTO transfer_leases VALUES (?, ?, ?) ON CONFLICT(job) DO UPDATE SET owner=excluded.owner, expires=excluded.expires WHERE transfer_leases.expires<=?',
    job, owner, now + TTL, now,
  );
  if (claimed.changes !== 1) return null;
  let alive = true;
  let lastRenewed = now;
  function renew() {
    const now = Date.now();
    alive = alive && db().runSync('UPDATE transfer_leases SET expires=? WHERE job=? AND owner=? AND expires>?', now + TTL, job, owner, now).changes === 1;
    lastRenewed = now;
    return alive;
  }
  const timer = setInterval(() => { try { renew(); } catch { alive = false; } }, 10_000);
  return {
    latest(): TransferRecord | undefined {
      const row = db().getFirstSync<{ record: string }>('SELECT record FROM transfer_checkpoints WHERE server=? AND id=? AND deleted=0', server, id);
      return row ? JSON.parse(row.record) as TransferRecord : undefined;
    },
    checkpoint(record: TransferRecord) {
      if (record.id !== id) throw new Error('Неверное задание очереди.');
      const now = Date.now();
      const result = db().runSync('INSERT INTO transfer_checkpoints(server,id,record) SELECT ?,?,? WHERE EXISTS(SELECT 1 FROM transfer_leases WHERE job=? AND owner=? AND expires>?) ON CONFLICT(server,id) DO UPDATE SET record=excluded.record WHERE deleted=0 AND EXISTS(SELECT 1 FROM transfer_leases WHERE job=? AND owner=? AND expires>?)', server,id,JSON.stringify(record),job,owner,now,job,owner,now);
      if (result.changes !== 1) throw new TransferLeaseLost('Владение заданием очереди потеряно.');
    },
    assertActive() {
      if (Date.now() - lastRenewed >= 1000) renew();
      if (!alive) throw new TransferLeaseLost('Задание уже передано другому обработчику.');
    },
    release() {
      clearInterval(timer);
      try { db().runSync('DELETE FROM transfer_leases WHERE job=? AND owner=?', job, owner); } catch { /* Expiry still permits recovery. */ }
    },
  };
}
