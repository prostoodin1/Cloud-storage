"use strict";
const $=id=>document.getElementById(id);
const token=decodeURIComponent(location.pathname.split("/").pop()||"");
const api=`/v1/public/shares/${encodeURIComponent(token)}`;
const download=`${api}/download`;
function bytes(n){if(n<1024)return`${n} Б`;const u=["КБ","МБ","ГБ","ТБ"];let i=-1;do{n/=1024;i++;}while(n>=1024&&i<3);return`${n.toFixed(1)} ${u[i]}`;}
async function start(){
  try{
    const response=await fetch(api,{cache:"no-store"});
    if(!response.ok)throw new Error(response.status===404?"Ссылка недействительна, отозвана или истекла.":"Не удалось открыть общий файл.");
    const info=await response.json();
    $("name").textContent=info.name;
    const expires=typeof info.expires_at==="number"?info.expires_at*1000:info.expires_at;
    $("meta").textContent=info.type==="file"?`${bytes(info.size_bytes||0)} · ссылка действует до ${new Date(expires).toLocaleString()}`:`Папка · ссылка действует до ${new Date(expires).toLocaleString()}`;
    const action=$("download");action.href=download;action.hidden=false;action.download=info.name;
    if(info.type!=="file"){$("preview").textContent="Эта ссылка не ведёт на отдельный файл.";action.hidden=true;return;}
    const type=(info.content_type||"").split(";")[0].toLowerCase();
    if(type.startsWith("image/")){const img=document.createElement("img");img.src=download;img.alt=info.name;$("preview").append(img);}
    else if(type.startsWith("video/")){const video=document.createElement("video");video.src=download;video.controls=true;video.preload="metadata";$("preview").append(video);}
    else if(type.startsWith("audio/")){const audio=document.createElement("audio");audio.src=download;audio.controls=true;audio.preload="metadata";$("preview").append(audio);}
    else if((type.startsWith("text/")||type==="application/json")&&(info.size_bytes||0)<=5*1024*1024){const payload=await fetch(download,{cache:"no-store"});if(!payload.ok)throw new Error("Не удалось загрузить содержимое файла.");const pre=document.createElement("pre");pre.textContent=await payload.text();$("preview").append(pre);}
    else{$("preview").textContent="Для этого формата доступно безопасное скачивание.";}
  }catch(error){$("name").textContent="Файл недоступен";$("preview").hidden=true;$("error").textContent=error.message;$("error").hidden=false;}
}
start();
