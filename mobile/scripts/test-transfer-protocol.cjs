// Protocol/queue tests with native file operations stubbed; not a device test.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const ts = require('typescript');
const MiB = 1024 ** 2;

function loadApi(fetch) {
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/api.ts'), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  vm.runInNewContext(code, { exports, URL, AbortController, TypeError,
    setTimeout: (fn, ms) => ms === 2000 ? (queueMicrotask(fn), 0) : setTimeout(fn, ms),
    clearTimeout,
    require(name) {
      if (name === 'expo/fetch') return { fetch };
      throw new Error(name);
    },
  });
  return exports;
}

test('mobile completion polls verification and keeps its owner active', async () => {
  const calls = [];
  const replies = [{ completion_pending: true }, { completion_pending: true }, { id: 'file' }];
  const { CloudApi } = loadApi(async (url, init) => {
    calls.push({ url, init });
    return { ok: true, text: async () => JSON.stringify(replies.shift()) };
  });
  let activeChecks = 0;
  const api = new CloudApi('https://cloud.example');
  const result = await api.completeUpload('session', () => { activeChecks += 1; });
  assert.equal(result.id, 'file');
  assert.equal(calls.length, 3);
  assert.equal(activeChecks, 6);
  assert.ok(calls.every(c => c.init.headers.Prefer === 'respond-async'));
});

test('mobile finalization stops when its queue owner is lost', async () => {
  let requests = 0;
  const { CloudApi } = loadApi(async () => {
    requests += 1;
    return { ok: true, text: async () => JSON.stringify({ completion_pending: true }) };
  });
  const api = new CloudApi('https://cloud.example');
  let activeChecks = 0;
  await assert.rejects(api.completeUpload('session', () => {
    if (++activeChecks === 3) throw new Error('lease lost');
  }), /lease lost/);
  assert.equal(requests, 1);
});

function load(total = 40 * MiB, downloadSize = total, downloadFailure = false, remoteBytes = null) {
  const files = new Map([['source', total], ['destination', 99]]);
  const contents = new Map();
  class ApiError extends Error { constructor(status,message=String(status),retryAfterMs) { super(message); this.status = status; this.retryAfterMs=retryAfterMs; } }
  class File {
    constructor(uri) { this.uri = uri; }
    get exists() { return files.has(this.uri); }
    get size() { return files.get(this.uri) ?? 0; }
    get type() { return 'application/octet-stream'; }
    slice(start, end) { return { size: end - start }; }
    delete() { files.delete(this.uri); }
    create() { files.set(this.uri, 0); contents.set(this.uri, Buffer.alloc(0)); }
    open() {
      let offset = 0;
      const uri = this.uri;
      return {
        get offset() { return offset; }, set offset(n) { offset = n; }, close() {},
        readBytes(n) { const b = contents.get(uri).subarray(offset, offset+n); offset += b.length; return b; },
        writeBytes(bytes) {
          const old = contents.get(uri) ?? Buffer.alloc(0);
          const next = Buffer.alloc(Math.max(old.length, offset + bytes.length));
          old.copy(next); Buffer.from(bytes).copy(next, offset); offset += bytes.length;
          contents.set(uri,next); files.set(uri,next.length);
        },
      };
    }
    async move(target) { files.set(target.uri, this.size); if(contents.has(this.uri)) contents.set(target.uri,contents.get(this.uri)); files.delete(this.uri); this.uri = target.uri; }
    static createDownloadTask(url, target) {
      return { downloadAsync: async () => {
        files.set(target.uri, downloadSize);
        if (remoteBytes) contents.set(target.uri, remoteBytes);
        if (downloadFailure) throw new TypeError('network lost');
        return target;
      } };
    }
  }
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/transfers.ts'), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  vm.runInNewContext(code, { exports, TypeError, setTimeout, clearTimeout, require(name) {
    if (name === '@noble/hashes/sha2.js') return require(name);
    if (name === 'expo-file-system') return { File, Directory: class {}, Paths: {} };
    if (name === './api') return { ApiError };
    throw new Error(name);
  } });
  return { ...exports, files, contents, ApiError };
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

function responseRange(bytes, offset, total, fail = false) {
  let delivered = false;
  return { status:206, headers: { get: name => name === 'Content-Range' ? `bytes ${offset}-${total-1}/${total}` : null },
    body: { getReader: () => ({
      async read() {
        if (!delivered) { delivered=true; return {done:false,value:fail ? bytes.subarray(0,2) : bytes}; }
        if (fail) throw new TypeError('network interrupted');
        return {done:true};
      }, async cancel() {}, releaseLock() {},
    }) },
  };
}

test('mobile download resumes the actual saved bytes and checks SHA-256', async () => {
  const payload=Buffer.from('0123456789');
  const mod=load(payload.length);
  const record={...initial,direction:'download',localUri:'destination',totalBytes:payload.length,
    expectedSha256:require('node:crypto').createHash('sha256').update(payload).digest('hex')};
  let fail=true;
  const offsets=[];
  const api={downloadRange:async (_,__,offset) => {
    offsets.push(offset); const current=fail; fail=false;
    return responseRange(payload.subarray(offset),offset,payload.length,current);
  }};
  await assert.rejects(mod.runDownloadTransfer(api,record,()=>{}));
  assert.equal(mod.files.get('destination'),99);
  const result=await mod.runDownloadTransfer(api,record,()=>{});
  assert.deepEqual(offsets,[0,2]);
  assert.equal(result.status,'completed');
  assert.equal(mod.contents.get('destination').toString(),payload.toString());
});

test('same-size corrupt mobile download cannot replace an existing copy', async () => {
  const payload=Buffer.from('0123456789');
  const mod=load(payload.length);
  const record={...initial,direction:'download',localUri:'destination',totalBytes:payload.length,
    expectedSha256:require('node:crypto').createHash('sha256').update(payload).digest('hex')};
  const api={downloadRange:async ()=>responseRange(Buffer.from('corruption'),0,payload.length)};
  await assert.rejects(mod.runDownloadTransfer(api,record,()=>{}),/SHA-256/);
  assert.equal(mod.files.get('destination'),99);
});

test('SQLite lease excludes foreground/background duplication and recovers expiry', t => {
  const { DatabaseSync }=require('node:sqlite');
  const os=require('node:os');
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'cloud-storage-leases-'));
  const handles=[],timers=[]; let now=1000;
  t.after(()=>{timers.forEach(clearInterval);handles.forEach(d=>d.close());fs.rmSync(dir,{recursive:true});});
  const code=ts.transpileModule(fs.readFileSync(path.join(__dirname,'../src/transferLease.ts'),'utf8'),{
    compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022},
  }).outputText;
  function runtime() {
    const exports={};
    vm.runInNewContext(code,{exports,Date:{now:()=>now},setInterval:(...args)=>{const id=setInterval(...args);timers.push(id);return id;},clearInterval,
      require:()=>({openDatabaseSync:()=>{
        const d=new DatabaseSync(path.join(dir,'leases.db'));handles.push(d);
        return {execSync:q=>d.exec(q),runSync:(q,...args)=>d.prepare(q).run(...args),
          getFirstSync:(q,...args)=>d.prepare(q).get(...args),getAllSync:(q,...args)=>d.prepare(q).all(...args),closeSync:()=>d.close()};
      }}),
    });return exports;
  }
  const foreground=runtime(),background=runtime();
  const first=foreground.claimTransfer('https://cloud.example','one');
  assert.ok(first);
  first.checkpoint({...initial,id:'one',completedBytes:3});
  background.mergeTransferRecords('https://cloud.example',[{...initial,id:'two'}]);
  foreground.mergeTransferRecords('https://cloud.example',[{...initial,id:'one',completedBytes:0}]);
  assert.equal(background.mergeTransferRecords('https://cloud.example',[]).length,2);
  assert.equal(background.mergeTransferRecords('https://cloud.example',[]).find(x=>x.id==='one').completedBytes,3);
  assert.equal(background.claimTransfer('https://cloud.example','one'),null);
  now+=61000;
  const second=background.claimTransfer('https://cloud.example','one');
  assert.ok(second);
  assert.equal(second.latest().completedBytes,3);
  assert.throws(()=>first.assertActive());
  assert.throws(()=>first.checkpoint({...initial,id:'one'}));
  second.checkpoint({...initial,id:'one',status:'completed'});
  first.release();
  assert.equal(foreground.claimTransfer('https://cloud.example','one'),null);
  second.release();
  foreground.clearCompletedRecords('https://cloud.example');
  foreground.mergeTransferRecords('https://cloud.example',[{...initial,id:'one'}]);
  assert.equal(foreground.mergeTransferRecords('https://cloud.example',[]).find(x=>x.id==='one'),undefined);
  foreground.resetTransferRecord('https://cloud.example',{...initial,id:'one'});
  const third=foreground.claimTransfer('https://cloud.example','one');
  assert.ok(third);third.release();
});
