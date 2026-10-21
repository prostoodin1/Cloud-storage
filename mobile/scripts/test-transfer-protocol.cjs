// Protocol/queue tests with native file operations stubbed; not a device test.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const ts = require('typescript');
const MiB = 1024 ** 2;

function load(total = 40 * MiB, downloadSize = total, downloadFailure = false) {
  const files = new Map([['source', total], ['destination', 99]]);
  class ApiError extends Error { constructor(status) { super(String(status)); this.status = status; } }
  class File {
    constructor(uri) { this.uri = uri; }
    get exists() { return files.has(this.uri); }
    get size() { return files.get(this.uri) ?? 0; }
    get type() { return 'application/octet-stream'; }
    slice(start, end) { return { size: end - start }; }
    delete() { files.delete(this.uri); }
    async move(target) { files.set(target.uri, this.size); files.delete(this.uri); this.uri = target.uri; }
    static createDownloadTask(url, target) {
      return { downloadAsync: async () => {
        files.set(target.uri, downloadSize);
        if (downloadFailure) throw new TypeError('network lost');
        return target;
      } };
    }
  }
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/transfers.ts'), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  vm.runInNewContext(code, { exports, TypeError, require(name) {
    if (name === 'expo-file-system') return { File, Directory: class {}, Paths: {} };
    if (name === './api') return { ApiError };
    throw new Error(name);
  } });
  return { ...exports, files, ApiError };
}

const initial = { id: 'job', direction: 'upload', localUri: 'source', logicalPath: 'file.bin',
  spaceId: 'space', completedBytes: 0, totalBytes: 40 * MiB, status: 'queued' };

for (const advertised of [undefined, 32 * MiB]) {
  test(`upload negotiates ${advertised ?? 'old-server fallback'} and preserves bytes`, async () => {
    const mod = load();
    const chunks = [];
    const api = {
      createUpload: async () => ({ id: 'session', received_bytes: 0, max_chunk_bytes: advertised }),
      uploadChunk: async (_, offset, chunk) => { chunks.push(chunk.size); return offset + chunk.size; },
      completeUpload: async () => ({}),
    };
    const result = await mod.runUploadTransfer(api, initial, () => {});
    assert.equal(result.status, 'completed');
    assert.equal(chunks.reduce((a, b) => a + b, 0), initial.totalBytes);
    assert.equal(Math.max(...chunks), advertised ? 16 * MiB : 4 * MiB);
    assert.equal(mod.files.has('source'), false);
  });
}

test('lost acknowledgement resumes from server offset and keeps unfinished source', async () => {
  const mod = load();
  const starts = [];
  const api = {
    uploadStatus: async () => ({ id: 'session', received_bytes: 16 * MiB, max_chunk_bytes: 32 * MiB }),
    uploadChunk: async (_, offset, chunk) => { starts.push(offset); return offset + chunk.size; },
  };
  const result = await mod.runUploadTransfer(api, { ...initial, remoteUploadId: 'session' }, () => {}, 1);
  assert.deepEqual(starts, [16 * MiB]);
  assert.equal(result.status, 'queued');
  assert.equal(mod.files.has('source'), true);
});

test('bad acknowledgement or busy server cannot delete upload source', async () => {
  const mod = load();
  const api = {
    createUpload: async () => ({ id: 'session', received_bytes: 0 }),
    uploadChunk: async () => 0,
  };
  await assert.rejects(mod.runUploadTransfer(api, initial, () => {}), /смещение/);
  assert.equal(mod.files.has('source'), true);
  assert.equal(mod.isRetryableTransferError(new mod.ApiError(503)), true);
  assert.equal(mod.isRetryableTransferError(new mod.ApiError(403)), false);
  assert.equal(mod.isRetryableTransferError(new TypeError('offline')), true);
});

for (const fail of [true, false]) {
  test(`failed/incomplete download preserves existing destination (${fail})`, async () => {
    const mod = load(40 * MiB, 3, fail);
    await assert.rejects(mod.runDownloadTransfer({ fileUrl: () => 'url', fileHeaders: () => ({}) },
      { ...initial, direction: 'download', localUri: 'destination' }, () => {}));
    assert.equal(mod.files.get('destination'), 99);
  });
}

test('only a complete download replaces the existing file', async () => {
  const mod = load();
  const result = await mod.runDownloadTransfer({ fileUrl: () => 'url', fileHeaders: () => ({}) },
    { ...initial, direction: 'download', localUri: 'destination' }, () => {});
  assert.equal(result.status, 'completed');
  assert.equal(mod.files.get('destination'), 40 * MiB);
});
