"use strict";
const $ = id => document.getElementById(id);
let csrf = "", spaces = [], current = null, directory = "", previewURL = null;
function status(message, error = false) { $("status").textContent = message; $("status").classList.toggle("error", error); }
function loginView() { $("login").hidden = false; $("workspace").hidden = true; $("logout").hidden = true; $("entries").replaceChildren(); closePreview(); csrf = ""; }
const translations = {"remote pairing is disabled by the administrator":"В Manager отключён вход через интернет. Разрешите первичное подключение устройств.","dynamic pairing code is invalid or expired":"Код недействителен или истёк. Скопируйте новый код из Manager.","too many pairing attempts":"Слишком много попыток. Подождите перед повторным входом.","server is in emergency read-only mode":"Сервер работает в режиме только чтения.","storage roots are not configured":"Администратор ещё не настроил диски хранения."};
async function api(url, options = {}) {
  const response = await fetch(url, {credentials:"same-origin", cache:"no-store", ...options, headers:{"X-CSRF-Token":csrf, "skip_zrok_interstitial":"1", ...options.headers}});
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    if (response.status === 401) loginView();
    let detail = typeof data.detail === "string" ? data.detail : `Ошибка сервера (${response.status})`;
    throw new Error(translations[detail] || detail);
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
  spaces=await (await api("/v1/spaces")).json();
  $("login").hidden=true;$("workspace").hidden=false;$("logout").hidden=false;
  $("spaces").replaceChildren();
  for(const space of spaces) $("spaces").append(button(space.name,()=>selectSpace(space)));
  if(!spaces.length){current=null;$("entries").replaceChildren();$("empty").hidden=false;$("empty").textContent="Нет доступных пространств. Обратитесь к администратору.";$("upload").disabled=$("new-folder").disabled=true;status("Подключение установлено");return;}
  const selected=spaces.find(s=>current && s.id===current.id)||spaces[0];await selectSpace(selected);
}
async function selectSpace(space) {current=space;directory="";$("space-name").textContent=space.name;$("quota").textContent=`Квота ${bytes(space.quota_bytes)}`;[...$("spaces").children].forEach((b,i)=>b.classList.toggle("active",spaces[i].id===space.id));await refresh();}
async function refresh() {
  if(!current)return; status("Обновляем список файлов…");
  const all=await (await api("/v1/spaces")).json();
  const available=all.find(s=>s.id===current.id);
  if(!available){current=null;await loadSpaces();return;}
  current=available;
  const entries=await (await api(`${base()}/entries?directory=${encodeURIComponent(directory)}`)).json();
  $("entries").replaceChildren();$("up").disabled=!directory;$("upload").disabled=!current.can_upload;$("new-folder").disabled=!current.can_upload;
  $("breadcrumbs").textContent=`${current.name} / ${directory || "Корень"}`;
  entries.sort((a,b)=>(a.type===b.type?a.name.localeCompare(b.name):a.type==="directory"?-1:1));
  for(const entry of entries) {
    const row=document.createElement("tr"), name=document.createElement("td"), size=document.createElement("td"), date=document.createElement("td"), actions=document.createElement("td");
    name.append(button(`${entry.type==="directory"?"▸":"▤"} ${entry.name}`,async()=>{if(entry.type==="directory"){directory=joined(entry.name);await refresh();}else await preview(entry);},"filename"));
    size.textContent=entry.type==="directory"?"—":bytes(entry.size_bytes);
    date.textContent=entry.modified_at?new Date(entry.modified_at).toLocaleString():"—";
    if(entry.type==="file")actions.append(button("Скачать",()=>download(entry)));
    if(current.can_modify)actions.append(button("Переименовать",()=>rename(entry)));
    if(current.can_delete)actions.append(button("Удалить",()=>remove(entry)));
    row.append(name,size,date,actions);$("entries").append(row);
  }
  $("empty").textContent="В этой папке пока нет файлов";$("empty").hidden=entries.length>0;status(`Подключено · Объектов: ${entries.length}`);
}
function download(entry) {const a=document.createElement("a");a.href=`${base()}/files/${encoded(joined(entry.name))}`;a.download=entry.name;document.body.append(a);a.click();a.remove();status(`Скачивание: ${entry.name}`);}
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
$("pair-form").addEventListener("submit",event=>{event.preventDefault();run(async()=>{$("connect").disabled=true;try{status("Подключаемся…");const data=await(await json("/v1/web/pair","POST",{code:$("code").value.trim()})).json();csrf=data.csrf;$("code").value="";await loadSpaces();}finally{$("connect").disabled=false;}});});
$("logout").addEventListener("click",()=>run(async()=>{await json("/v1/web/logout","POST",{});loginView();status("Вы вышли из облака на этом устройстве");}));
$("refresh").addEventListener("click",()=>run(refresh));
$("up").addEventListener("click",()=>run(async()=>{directory=directory.split("/").slice(0,-1).join("/");await refresh();}));
$("new-folder").addEventListener("click",()=>run(async()=>{const name=await ask("Название новой папки");if(name===null)return;await json(`${base()}/directories`,"POST",{logical_path:joined(validName(name))});await refresh();}));
$("upload").addEventListener("click",()=>$("file-input").click());
$("file-input").addEventListener("change",()=>run(async()=>{const selected=[...$("file-input").files], targetBase=base(), targetDir=directory;$("file-input").value="";$("upload").disabled=true;try{for(let i=0;i<selected.length;i++){const file=selected[i], target=targetDir?`${targetDir}/${validName(file.name)}`:validName(file.name);status(`Загружаем ${i+1}/${selected.length}: ${file.name} (${bytes(file.size)})`);await api(`${targetBase}/files/${encoded(target)}`,{method:"PUT",headers:{"Content-Type":file.type||"application/octet-stream"},body:file});}await refresh();status(`Загрузка завершена · Файлов: ${selected.length}`);}finally{$("upload").disabled=!current?.can_upload;}}));
$("close-preview").addEventListener("click",closePreview);
$("preview").addEventListener("cancel",closePreview);
window.addEventListener("pageshow",()=>run(async()=>{try{const session=await(await api("/v1/web/session")).json();csrf=session.csrf;$("server-name").textContent=session.server_name;await loadSpaces();}catch(error){loginView();status(error.message,true);}}));
