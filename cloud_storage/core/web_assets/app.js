"use strict";
const $ = id => document.getElementById(id);
let csrf = "", spaces = [], current = null, directory = "", previewURL = null, navigation = 0;
let uploadOwner = "", uploadRunning = false;
const uploadChunkSize = 8 * 1024 * 1024;
let renderedFolder = "";
function status(message, error = false) { $("status").textContent = message; $("status").classList.toggle("error", error); }
function loginView() { navigation++; current=null; directory=""; $("login").hidden = false; $("workspace").hidden = true; $("logout").hidden = true; $("entries").replaceChildren(); closePreview(); csrf = ""; }
const translations = {"remote pairing is disabled by the administrator":"В Manager отключён вход через интернет. Разрешите первичное подключение устройств.","dynamic pairing code is invalid or expired":"Код недействителен или истёк. Скопируйте новый код из Manager.","invalid username or password":"Неверный логин или пароль.","too many pairing attempts":"Слишком много попыток. Подождите перед повторным входом.","server is in emergency read-only mode":"Сервер работает в режиме только чтения.","storage roots are not configured":"Администратор ещё не настроил диски хранения."};
async function api(url, options = {}) {
  const response = await fetch(url, {credentials:"same-origin", cache:"no-store", ...options, headers:{"X-CSRF-Token":csrf, "skip_zrok_interstitial":"1", ...options.headers}});
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    if (response.status === 401 || (response.status === 403 && ["/v1/spaces","/v1/web/session"].includes(url))) loginView();
    let detail = typeof data.detail === "string" ? data.detail : `Ошибка сервера (${response.status})`;
    throw Object.assign(new Error(translations[detail] || detail), {status: response.status});
  }
  return response;
}
const json = (url, method, body) => api(url, {method, headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
async function ask(title, initial = "", confirmation = false) {
  const dialog=$("edit-dialog"), input=$("edit-name");
  $("edit-title").textContent=title; input.value=initial;input.hidden=confirmation;input.required=!confirmation;$("edit-label").hidden=confirmation;
  $("edit-ok").textContent=confirmation?"Удалить":"Сохранить";dialog.returnValue="cancel";dialog.showModal();if(!confirmation)input.focus();
  return new Promise(resolve=>dialog.addEventListener("close",()=>resolve(dialog.returnValue==="ok"?(confirmation?true:input.value):null),{once:true}));
}
const base = () => `/v1/spaces/${encodeURIComponent(current.id)}`;
const joined = name => directory ? `${directory}/${name}` : name;
const encoded = path => path.split("/").map(encodeURIComponent).join("/");
function bytes(n) { if(n < 1024) return `${n} Б`; const units=["КБ","МБ","ГБ","ТБ"];let i=-1;do{n/=1024;i++;}while(n>=1024&&i<3);return `${n.toFixed(1)} ${units[i]}`; }
function button(text, action, className = "") { const b=document.createElement("button"); b.textContent=text;b.className=className;b.addEventListener("click",()=>run(action));return b; }
async function run(action) { try { await action(); } catch(error) { status(error.message || "Нет связи с сервером", true); } }
function validName(name) { if(!name || !name.trim() || /[\\/\x00-\x1f]/.test(name) || [".",".."].includes(name)) throw new Error("Введите имя без слешей и служебных символов");return name; }
async function loadSpaces() {
  const session = await (await api("/v1/web/session")).json();
  uploadOwner = session.device.user_id;
  spaces=await (await api("/v1/spaces")).json();
  $("login").hidden=true;$("workspace").hidden=false;$("logout").hidden=false;
  renderSpaces();
  if(!spaces.length){current=null;directory="";$("space-name").textContent="Нет доступных пространств";$("quota").textContent="";$("breadcrumbs").textContent="";$("up").disabled=true;$("entries").replaceChildren();$("empty").hidden=false;$("empty").textContent="Нет доступных пространств. Обратитесь к администратору.";$("upload").disabled=$("new-folder").disabled=true;status("Подключение установлено");return;}
  const selected=spaces.find(s=>current && s.id===current.id)||spaces[0];await selectSpace(selected);
}
function renderSpaces() {$("spaces").replaceChildren();for(const space of spaces) $("spaces").append(button(space.name,()=>selectSpace(space),space.id===current?.id?"active":""));}
async function selectSpace(space) {if(!current||current.id!==space.id){directory="";$("entries").replaceChildren();}current=space;$("space-name").textContent=space.name;[...$("spaces").children].forEach((b,i)=>b.classList.toggle("active",spaces[i].id===space.id));await refresh();}
async function refresh() {
  if(!current)return; status("Обновляем список файлов…");
  const requestId=++navigation, spaceId=current.id, requestedDirectory=directory;
  const all=await (await api("/v1/spaces")).json();
  if(requestId!==navigation||!current||spaceId!==current.id||requestedDirectory!==directory)return;
  spaces=all;renderSpaces();
  const available=all.find(s=>s.id===spaceId);
  if(!available){current=null;await loadSpaces();return;}
  current=available;
  const entries=await (await api(`/v1/spaces/${encodeURIComponent(spaceId)}/entries?directory=${encodeURIComponent(requestedDirectory)}`)).json();
  if(requestId!==navigation||!current||spaceId!==current.id||requestedDirectory!==directory)return;
  $("quota").textContent=`Занято ${bytes(current.used_bytes||0)} из ${bytes(current.quota_bytes)} · свободно ${bytes(current.free_bytes ?? current.quota_bytes)}`;
  const folderKey = JSON.stringify([spaceId, requestedDirectory]);
  const table = $("entries").closest(".table-wrap");
  const sameFolder = renderedFolder === folderKey;
  const scroll = sameFolder ? [table.scrollLeft, table.scrollTop, window.scrollY] : [0, 0, null];
  const focusedFile = sameFolder ? document.activeElement?.dataset.fileName : undefined;
  renderedFolder = folderKey;
  $("entries").replaceChildren();$("up").disabled=!directory;$("upload").disabled=!current.can_upload;$("new-folder").disabled=!current.can_upload;
  $("breadcrumbs").textContent=`${current.name} / ${directory || "Корень"}`;
  entries.sort((a,b)=>(a.type===b.type?a.name.localeCompare(b.name):a.type==="directory"?-1:1));
  for(const entry of entries) {
    const row=document.createElement("tr"), name=document.createElement("td"), size=document.createElement("td"), date=document.createElement("td"), actions=document.createElement("td");
    const fileButton = button(`${entry.type==="directory"?"▸":"▤"} ${entry.name}`,async()=>{if(entry.type==="directory"){directory=joined(entry.name);await refresh();}else await preview(entry);},"filename");
    fileButton.dataset.fileName = entry.name;
    name.append(fileButton);
    size.textContent=entry.type==="directory"?"—":bytes(entry.size_bytes);
    date.textContent=entry.modified_at?new Date(entry.modified_at).toLocaleString():"—";
    if(entry.type==="file")actions.append(button("Скачать",()=>download(entry)));
    if(entry.type==="file"&&current.can_share)actions.append(button("↗ Поделиться",()=>share(entry)));
    if(current.can_modify)actions.append(button("Переименовать",()=>rename(entry)));
    if(current.can_delete)actions.append(button("Удалить",()=>remove(entry)));
    row.append(name,size,date,actions);$("entries").append(row);
    if (focusedFile === entry.name) fileButton.focus({preventScroll: true});
  }
  table.scrollTo(scroll[0], scroll[1]);
  if (scroll[2] !== null) window.scrollTo(window.scrollX, scroll[2]);
  $("empty").textContent="В этой папке пока нет файлов";$("empty").hidden=entries.length>0;status(`Подключено · Объектов: ${entries.length}`);
}
function download(entry) {const a=document.createElement("a");a.href=`${base()}/files/${encoded(joined(entry.name))}`;a.download=entry.name;document.body.append(a);a.click();a.remove();status(`Скачивание: ${entry.name}`);}
async function share(entry) {const response=await json("/v1/web/shares","POST",{space_id:current.id,logical_path:joined(entry.name),kind:entry.type,ttl_hours:168});const data=await response.json();const link=data.url||`${location.origin}${data.page_url_path}`;await navigator.clipboard.writeText(link);status("Ссылка скопирована. Она показывает только выбранный объект и действует 7 дней.");}
async function rename(entry) {const name=await ask("Новое имя",entry.name);if(name===null||name===entry.name)return;await json(`${base()}/moves`,"POST",{source_path:joined(entry.name),destination_path:joined(validName(name)),kind:entry.type});await refresh();}
async function remove(entry) {if(!await ask(`Удалить «${entry.name}»?`,"",true))return;await api(`${base()}/${entry.type==="directory"?"directories":"files"}/${encoded(joined(entry.name))}`,{method:"DELETE"});await refresh();}
function closePreview(){if(previewURL){URL.revokeObjectURL(previewURL);previewURL=null;}$("preview-content").replaceChildren();$("preview").close();}
async function preview(entry) {
  const type=(entry.content_type||"").split(";")[0].toLowerCase(), image=["image/png","image/jpeg","image/gif","image/webp","image/avif"].includes(type), text=type.startsWith("text/")||type==="application/json";
  if((!image&&!text)||entry.size_bytes>10*1024*1024){status("Для этого файла доступно скачивание (просмотр: изображения и текст до 10 МБ)");return;}
  closePreview();const response=await api(`${base()}/files/${encoded(joined(entry.name))}`);const payload=await response.blob();
  if(image){previewURL=URL.createObjectURL(new Blob([payload],{type}));const img=document.createElement("img");img.alt=entry.name;img.src=previewURL;$("preview-content").append(img);}
  else {const pre=document.createElement("pre");pre.textContent=await payload.text();$("preview-content").append(pre);}
  $("preview-title").textContent=entry.name;$("preview").showModal();
}
$("login-form").addEventListener("submit",event=>{event.preventDefault();run(async()=>{$("login-button").disabled=true;try{status("Входим в аккаунт…");const data=await(await json("/v1/web/login","POST",{username:$("username").value.trim(),password:$("password").value,remember:$("remember-login").checked})).json();csrf=data.csrf;$("password").value="";await loadSpaces();}finally{$("login-button").disabled=false;}});});
$("pair-form").addEventListener("submit",event=>{event.preventDefault();run(async()=>{$("connect").disabled=true;try{status("Подключаемся…");const data=await(await json("/v1/web/pair","POST",{code:$("code").value.trim(),remember:$("remember-pair").checked})).json();csrf=data.csrf;$("code").value="";await loadSpaces();if(data.initial_username&&data.initial_password){$("credentials-text").textContent=`Логин: ${data.initial_username}\nПароль: ${data.initial_password}`;$("new-credentials").showModal();}}finally{$("connect").disabled=false;}});});
$("copy-credentials").addEventListener("click",()=>run(async()=>{await navigator.clipboard.writeText($("credentials-text").textContent);status("Логин и пароль скопированы");}));
$("close-credentials").addEventListener("click",()=>$("new-credentials").close());
$("logout").addEventListener("click",()=>run(async()=>{await json("/v1/web/logout","POST",{});loginView();status("Вы вышли из облака на этом устройстве");}));
$("refresh").addEventListener("click",()=>run(refresh));
$("up").addEventListener("click",()=>run(async()=>{directory=directory.split("/").slice(0,-1).join("/");await refresh();}));
$("new-folder").addEventListener("click",()=>run(async()=>{const name=await ask("Название новой папки");if(name===null)return;await json(`${base()}/directories`,"POST",{logical_path:joined(validName(name))});await refresh();}));
$("upload").addEventListener("click",()=>$("file-input").click());

function showTransfer(file, received, message) {
  $("transfer-panel").hidden = false;
  $("transfer-name").textContent = file.name;
  $("transfer-progress").value = file.size ? 100 * received / file.size : 100;
  $("transfer-state").textContent = message;
}

async function uploadOne(file, targetBase, path) {
  if (file.size > 75_000_000_000) throw new Error("Максимальный размер файла — 75 ГБ");
  const key = JSON.stringify(["cloud-storage-upload", uploadOwner, targetBase, path, file.size]);
  let id = localStorage.getItem(key);
  let state;
  if (id) {
    try {
      state = await (await api(`/v1/uploads/${encodeURIComponent(id)}`)).json();
    } catch (error) {
      if (error.status !== 404) throw error;
      localStorage.removeItem(key);
      id = null;
    }
  }
  if (!id) {
    state = await (await json(`${targetBase}/uploads`, "POST", {
      logical_path: path, size_bytes: file.size, content_type: file.type || "application/octet-stream"
    })).json();
    id = state.id;
    localStorage.setItem(key, id);
  }
  const url = `/v1/uploads/${encodeURIComponent(id)}`;
  const received = state.received_bytes;
  if (!Number.isSafeInteger(received) || received < 0 || received > file.size) {
    throw new Error("Сервер вернул неверную позицию загрузки");
  }
  if (state.status === "active" && received) {
    showTransfer(file, received, "Сверяем уже принятые части файла…");
    for (let offset = 0; offset < received; offset += uploadChunkSize) {
      const end = Math.min(received, offset + uploadChunkSize);
      const payload = await file.slice(offset, end).arrayBuffer();
      const digest = await crypto.subtle.digest("SHA-256", payload);
      const sha256 = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
      const server = await (await api(`${url}/chunk-digest?offset=${offset}&length=${end - offset}`)).json();
      if (sha256 !== server.sha256) {
        if (window.confirm("Принятые части не совпадают. Удалить незавершённую загрузку и начать заново?")) {
          await api(url, {method: "DELETE"});
          localStorage.removeItem(key);
          return uploadOne(file, targetBase, path);
        }
        throw new Error("Выберите исходный файл, чтобы продолжить");
      }
    }
  }
  for (let offset = received; offset < file.size; offset += uploadChunkSize) {
    const end = Math.min(file.size, offset + uploadChunkSize);
    const response = await api(url, {method: "PATCH", headers: {
      "Content-Type": "application/offset+octet-stream", "Upload-Offset": String(offset)
    }, body: file.slice(offset, end)});
    if (Number(response.headers.get("Upload-Offset")) !== end) {
      throw new Error("Сервер не подтвердил позицию загрузки");
    }
    showTransfer(file, end, `Принято ${bytes(end)} из ${bytes(file.size)}`);
  }
  showTransfer(file, file.size, "Сервер проверяет и сохраняет файл…");
  while (true) {
    const response = await api(`${url}/complete`, {method: "POST", headers: {Prefer: "respond-async"}});
    if (response.status !== 202) break;
    await new Promise(resolve => setTimeout(resolve, 2000));
  }
  localStorage.removeItem(key);
  showTransfer(file, file.size, "Файл сохранён");
}

async function uploadFiles(files) {
  if (uploadRunning || !current?.can_upload || !files.length) return;
  const targetBase = base(), targetDirectory = directory;
  uploadRunning = true;
  $("upload").disabled = true;
  try {
    for (const file of files) {
      const name = validName(file.name);
      showTransfer(file, 0, "Готовим загрузку…");
      await uploadOne(file, targetBase, targetDirectory ? `${targetDirectory}/${name}` : name);
    }
    await refresh();
  } catch (error) {
    $("transfer-state").textContent = error.message || "Передача прервана";
    throw error;
  } finally {
    uploadRunning = false;
    $("upload").disabled = !current?.can_upload;
  }
}

$("file-input").addEventListener("change", () => {
  const files = Array.from($("file-input").files);
  $("file-input").value = "";
  run(() => uploadFiles(files));
});
const dropZone = $("workspace").querySelector(".files");
dropZone.addEventListener("dragover", event => event.preventDefault());
dropZone.addEventListener("drop", event => {
  event.preventDefault();
  run(() => uploadFiles(Array.from(event.dataTransfer.files)));
});
$("close-preview").addEventListener("click",closePreview);
$("preview").addEventListener("cancel",closePreview);
window.addEventListener("pageshow",()=>run(async()=>{try{const session=await(await api("/v1/web/session")).json();csrf=session.csrf;$("server-name").textContent=session.server_name;await loadSpaces();}catch(error){loginView();status(error.message,true);}}));
