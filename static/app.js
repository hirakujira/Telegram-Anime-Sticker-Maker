'use strict';
const $ = id => document.getElementById(id);
const state = {id:null, meta:null, index:null, first:1, last:1, start:0, end:1,
  mode:'square', x:0, y:0, generation:0, frameRequest:0, busy:false, previewBusy:false,
  rangePlaying:false, fallback:false, stillUrl:null};
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));
const number = id => Number($(id).value);
const video = $('video');

function timeLabel(seconds) {
  if (!Number.isFinite(seconds)) return '00:00.000';
  return `${String(Math.floor(seconds / 60)).padStart(2,'0')}:${(seconds % 60).toFixed(3).padStart(6,'0')}`;
}
function notice(message='') { $('notice').textContent=message; $('notice').hidden=!message; }
async function api(url, options={}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `請求失敗 (${response.status})`);
  return data;
}
const post = url => api(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
function nearest(values, target) {
  let lo=0, hi=values.length;
  while (lo<hi) { const mid=(lo+hi)>>1; if(values[mid]<target) lo=mid+1; else hi=mid; }
  if (lo===0) return 0;
  if (lo===values.length) return lo-1;
  return target-values[lo-1] <= values[lo]-target ? lo-1 : lo;
}
function endBoundary(frame) { return frame < state.index.count ? state.index.times[frame] : state.index.end_time; }
function nearestEnd(target) {
  const i=nearest(state.index.times, target);
  const candidate=clamp(i,1,state.index.count);
  // End choices are frame boundaries, not the beginning of the chosen last frame.
  const choices=[candidate, Math.min(candidate+1,state.index.count), state.index.count];
  return choices.reduce((a,b)=>Math.abs(endBoundary(a)-target)<=Math.abs(endBoundary(b)-target)?a:b);
}

async function refresh() {
  $('refresh').disabled=true;
  try {
    const data=await api('/api/videos');
    const list=$('videoList'); list.replaceChildren();
    for(const item of data.videos) {
      const button=document.createElement('button'); button.className='video-item'; button.dataset.id=item.id;
      const icon=document.createElement('span'); icon.className='file-icon'; icon.textContent='▤';
      const name=document.createElement('strong'); name.textContent=item.name;
      const size=document.createElement('small'); size.textContent=`${(item.bytes/1024/1024).toFixed(1)} MB`;
      button.append(icon,name,size); button.addEventListener('click',()=>selectVideo(item.id));
      button.classList.toggle('active',item.id===state.id); list.append(button);
    }
    $('libraryStatus').textContent=data.videos.length ? `${data.videos.length} 部影片` : '目前沒有影片。支援 MP4、MKV、MOV、WEBM 等格式。';
    if(state.id && !data.videos.some(item=>item.id===state.id)) {
      state.generation++; state.id=null; state.index=null; state.meta=null;
      video.pause(); video.removeAttribute('src'); video.load();
      $('viewport').hidden=true; $('emptyStage').hidden=false;
      $('rangeControls').disabled=true; $('settingsControls').disabled=true;
      $('play').disabled=true; $('playRange').disabled=true; $('fallback').disabled=true;
      notice('原影片已變更或移除，請重新選擇。');
    }
  } catch(error) { notice(error.message); }
  finally { $('refresh').disabled=false; }
}

async function selectVideo(id) {
  if(state.busy || state.previewBusy) { notice('請等待目前的工作完成後再切換影片。'); return; }
  const generation=++state.generation;
    state.id=id; state.index=null; state.meta=null; state.fallback=false; state.rangePlaying=false;
  state.frameRequest++; video.pause(); video.removeAttribute('src'); video.load(); hideStill();
  notice(); $('result').hidden=true; $('jobStatus').hidden=true;
  $('rangeControls').disabled=true; $('settingsControls').disabled=true;
  $('play').disabled=true; $('playRange').disabled=true; $('fallback').disabled=true;
    $('indexStatus').textContent='讀取影片資訊…';
    $('fallback').textContent='轉換播放格式';
  document.querySelectorAll('.video-item').forEach(el=>el.classList.toggle('active',el.dataset.id===id));
  try {
    const meta=await api(`/api/videos/${id}/metadata`);
    if(generation!==state.generation) return;
    state.meta=meta; state.start=0; state.end=Math.min(3,meta.duration);
    state.mode='square'; state.x=meta.width-Math.min(meta.width,meta.height);
    state.y=Math.floor((meta.height-Math.min(meta.width,meta.height))/2);
    $('speed').value=1;
    $('emptyStage').hidden=true; $('viewport').hidden=false;
    $('dimensions').textContent=`${meta.width} × ${meta.height}`;
    $('rangeControls').disabled=false; $('settingsControls').disabled=false;
    $('play').disabled=false; $('fallback').disabled=false;
    for(const key of ['startRange','endRange','startTime','endTime']) $(key).max=meta.duration;
    $('totalTime').textContent=timeLabel(meta.duration);
    setMode('square'); fitViewport(); updateRange();
    video.src=`/api/videos/${id}/media`; video.load();
    $('indexStatus').textContent='可先播放影片；正在讀取影格索引以啟用逐格微調…';
    setFrameControls(false);
    while(generation===state.generation) {
      const index=await api(`/api/videos/${id}/index`);
      if(generation!==state.generation) return;
      if(index.status==='failed') throw new Error(index.message);
      if(index.status==='ready') {
        state.index=index;
        state.meta.duration=index.end_time;
        state.first=nearest(index.times,state.start)+1;
        state.last=Math.max(state.first,nearestEnd(Math.min(state.end,index.end_time)));
        while(state.last>state.first && endBoundary(state.last)-index.times[state.first-1]>3*number('speed')+1e-9) state.last--;
        for(const key of ['startRange','endRange','startTime','endTime']) $(key).max=index.end_time;
        for(const key of ['startFrame','endFrame']) $(key).max=index.count;
        $('totalTime').textContent=timeLabel(index.end_time);
        $('indexStatus').textContent=`${index.count.toLocaleString()} 個原始影格 · 可逐格微調`;
        setFrameControls(true); $('playRange').disabled=false; updateRange(); break;
      }
      await delay(650);
    }
  } catch(error) {
    if(generation===state.generation) { notice(error.message); $('indexStatus').textContent='無法完成影格索引。'; }
  }
}

function setFrameControls(enabled) {
  for(const key of ['startFrame','endFrame','startPrev','startNext','endPrev','endNext','viewStart','viewEnd']) $(key).disabled=!enabled;
}
function updateRange() {
  if(!state.meta) return;
  if(state.index) { state.start=state.index.times[state.first-1]; state.end=endBoundary(state.last); }
  $('startTime').value=state.start.toFixed(3); $('endTime').value=state.end.toFixed(3);
  $('startRange').value=state.start; $('endRange').value=state.end;
  $('startFrame').value=state.first; $('endFrame').value=state.last;
  const total=state.meta.duration;
  $('rangeBand').style.left=`${state.start/total*100}%`;
  $('rangeBand').style.width=`${(state.end-state.start)/total*100}%`;
  updateSummary();
}
function changeTime(which, value) {
  if(!state.meta || !Number.isFinite(value)) return;
  value=clamp(value,0,state.meta.duration);
  if(state.index) {
    const span=state.end-state.start;
    if(which==='start') {
      state.first=nearest(state.index.times,value)+1;
      if(state.first>state.last) state.last=Math.max(state.first,nearestEnd(Math.min(state.meta.duration,state.index.times[state.first-1]+span)));
    } else {
      state.last=nearestEnd(value);
      if(state.last<state.first) state.first=Math.min(state.last,nearest(state.index.times,Math.max(0,endBoundary(state.last)-span))+1);
    }
  } else if(which==='start') {
    const span=state.end-state.start;
    state.start=Math.min(value,Math.max(0,state.meta.duration-.001));
    if(state.start>=state.end) state.end=Math.min(state.meta.duration,state.start+span);
  } else {
    const span=state.end-state.start;
    state.end=Math.max(value,.001);
    if(state.end<=state.start) state.start=Math.max(0,state.end-span);
  }
  updateRange();
  state.rangePlaying=false; video.pause(); hideStill();
  video.currentTime=which==='start'?state.start:(state.index?state.index.times[state.last-1]:Math.max(state.start,state.end-.001));
}
function changeFrame(which, value) {
  if(!state.index || !Number.isInteger(value)) { updateRange(); return; }
  if(which==='start') state.first=clamp(value,1,state.last);
  else state.last=clamp(value,state.first,state.index.count);
  updateRange(); showFrame(which==='start'?state.first:state.last);
}
function hideStill() {
  state.frameRequest++;
  $('still').hidden=true; video.hidden=false;
  if(state.stillUrl) { URL.revokeObjectURL(state.stillUrl); state.stillUrl=null; }
}
async function showFrame(number) {
  video.pause(); state.rangePlaying=false;
  const request=++state.frameRequest, generation=state.generation;
  $('indexStatus').textContent=`正在擷取原片第 ${number} 格…`;
  try {
    const response=await fetch(`/api/videos/${state.id}/frames/${number}`);
    if(!response.ok) throw new Error((await response.json()).error);
    const blob=await response.blob();
    if(request!==state.frameRequest || generation!==state.generation) return;
    const url=URL.createObjectURL(blob), image=new Image(); image.src=url;
    await image.decode();
    if(request!==state.frameRequest || generation!==state.generation) { URL.revokeObjectURL(url); return; }
    if(state.stillUrl) URL.revokeObjectURL(state.stillUrl);
    state.stillUrl=url; $('still').src=url; $('still').hidden=false; video.hidden=true;
    video.currentTime=state.index.times[number-1];
    $('indexStatus').textContent=`原片第 ${number.toLocaleString()} / ${state.index.count.toLocaleString()} 格 · ${timeLabel(state.index.times[number-1])}`;
  } catch(error) { if(generation===state.generation && request===state.frameRequest) notice(error.message); }
}

function fitViewport() {
  if(!state.meta) return;
  const stage=$('stage'), {width,height}=state.meta;
  const scale=Math.min(stage.clientWidth/width,stage.clientHeight/height);
  $('viewport').style.width=`${width*scale}px`; $('viewport').style.height=`${height*scale}px`;
  drawCrop();
}
function setMode(mode) {
  state.mode=mode;
  $('squareMode').classList.toggle('active',mode==='square'); $('squareMode').setAttribute('aria-pressed',mode==='square');
  $('originalMode').classList.toggle('active',mode==='original'); $('originalMode').setAttribute('aria-pressed',mode==='original');
  $('cropControls').hidden=mode!=='square'; $('cropBox').hidden=mode!=='square';
  drawCrop(); updateSummary();
}
function cropAxis() { return state.meta.width>=state.meta.height?'x':'y'; }
function maxOffset() { return Math.abs(state.meta.width-state.meta.height); }
function drawCrop() {
  if(!state.meta) return;
  const {width,height}=state.meta, side=Math.min(width,height), box=$('cropBox');
  box.style.left=`${state.x/width*100}%`; box.style.top=`${state.y/height*100}%`;
  box.style.width=`${side/width*100}%`; box.style.height=`${side/height*100}%`;
  const axis=cropAxis(), offset=state[axis], max=maxOffset();
  $('cropLabel').textContent=axis==='x'?'左側裁切像素':'上側裁切像素';
  $('cropOffset').max=max; $('cropRange').max=max; $('cropOffset').value=offset; $('cropRange').value=offset;
  $('cropBox').setAttribute('aria-valuemin','0'); $('cropBox').setAttribute('aria-valuemax',max);
  $('cropBox').setAttribute('aria-valuenow',offset);
  $('alignStart').textContent=axis==='x'?'靠左':'靠上'; $('alignEnd').textContent=axis==='x'?'靠右':'靠下';
  $('alignStart').classList.toggle('active',offset===0); $('alignCenter').classList.toggle('active',offset===Math.round(max/2));
  $('alignEnd').classList.toggle('active',offset===max);
}
function setOffset(value) {
  if(!state.meta || !Number.isFinite(value)) return;
  state[cropAxis()]=clamp(Math.round(value),0,maxOffset()); drawCrop();
}
let drag=null;
$('cropBox').addEventListener('pointerdown',event=>{
  if(!state.meta || state.busy || state.previewBusy) return;
  const box=$('cropBox'); box.setPointerCapture(event.pointerId);
  drag={clientX:event.clientX,clientY:event.clientY,x:state.x,y:state.y};
});
$('cropBox').addEventListener('pointermove',event=>{
  if(!drag) return;
  const rect=$('viewport').getBoundingClientRect(), axis=cropAxis();
  setOffset(axis==='x'?drag.x+(event.clientX-drag.clientX)/rect.width*state.meta.width:
    drag.y+(event.clientY-drag.clientY)/rect.height*state.meta.height);
});
for(const name of ['pointerup','pointercancel','lostpointercapture']) $('cropBox').addEventListener(name,()=>{drag=null;});
$('cropBox').addEventListener('keydown',event=>{
  if(!state.meta || state.busy || state.previewBusy) return;
  const amount=event.shiftKey?10:1;
  if(['ArrowLeft','ArrowUp','ArrowRight','ArrowDown'].includes(event.key)) {
    event.preventDefault(); setOffset(state[cropAxis()]+(['ArrowLeft','ArrowUp'].includes(event.key)?-amount:amount));
  }
});

function updateSummary() {
  if(!state.meta) return;
  const speed=number('speed'), duration=(state.end-state.start)/speed;
  const valid=Number.isFinite(speed)&&speed>0&&Number.isFinite(duration)&&duration<=3+1e-9;
  $('outputDuration').textContent=Number.isFinite(duration)&&duration>0?`${(Math.ceil(duration*30-1e-8)/30).toFixed(3)} 秒`:'—';
  const {width,height}=state.meta;
  const dims=state.mode==='square'?[512,512]:width>=height?[512,Math.max(1,Math.round(height/width*512))]:[Math.max(1,Math.round(width/height*512)),512];
  $('outputSize').textContent=dims.join(' × ');
  $('selectedFrames').textContent=state.index?`${state.last-state.first+1} 格`:'索引中…';
  $('durationWarning').hidden=valid;
  $('durationWarning').textContent=!Number.isFinite(speed)||speed<=0?'請輸入大於零的速度倍率。':`超過 3 秒。請縮短範圍，或調至至少 ${Math.ceil((state.end-state.start)/3*1000)/1000}×。`;
  $('makeSticker').disabled=!valid||!state.index||state.busy||state.previewBusy;
  document.querySelectorAll('[data-speed]').forEach(el=>el.classList.toggle('active',Number(el.dataset.speed)===speed));
}
function setBusy(busy) {
  state.busy=busy;
  $('rangeControls').disabled=busy; $('settingsControls').disabled=busy;
  $('fallback').disabled=busy||state.previewBusy;
  updateSummary();
}
async function pollJob(id, generation, onReady) {
  $('jobStatus').hidden=false;
  while(generation===state.generation) {
    const job=await api(`/api/jobs/${id}`);
    $('jobMessage').textContent=job.message; $('jobProgress').value=job.progress;
    if(job.status==='failed') throw new Error(job.message);
    if(job.status==='ready') { onReady(job); return; }
    await delay(700);
  }
}
async function fallbackPreview() {
  if(!state.id||state.previewBusy||state.busy||state.fallback) return;
  state.previewBusy=true; state.fallback=true; $('fallback').disabled=true; updateSummary();
  const generation=state.generation, current=video.currentTime||0;
  try {
    const {id}=await post(`/api/videos/${state.id}/preview`);
    await pollJob(id,generation,()=>{
      hideStill(); video.src=`/api/jobs/${id}/file`; video.load();
      video.addEventListener('loadedmetadata',()=>{video.currentTime=Math.min(current,video.duration);},{once:true});
      $('fallback').textContent='已使用相容預覽';
    });
  } catch(error) { notice(error.message); state.fallback=false; }
  finally { state.previewBusy=false; $('fallback').disabled=state.busy||state.fallback; updateSummary(); }
}
async function makeSticker() {
  if($('makeSticker').disabled) return;
  notice(); setBusy(true); $('result').hidden=true;
  const generation=state.generation;
  try {
    const options={start:state.first,end:state.last,speed:number('speed'),mode:state.mode,x:state.x,y:state.y};
    const {id}=await api(`/api/videos/${state.id}/stickers`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(options)});
    await pollJob(id,generation,job=>{
      $('resultVideo').src=`/api/jobs/${id}/file`;
      $('download').href=`/api/jobs/${id}/file?download=1`; $('download').download=job.filename;
      $('resultDetails').textContent=`${job.width} × ${job.height} · ${job.duration.toFixed(3)} 秒 · ${(job.bytes/1024).toFixed(1)} KiB`;
      $('result').hidden=false;
    });
  } catch(error) { notice(error.message); $('jobMessage').textContent=error.message; }
  finally { setBusy(false); }
}

$('refresh').addEventListener('click',refresh);
$('squareMode').addEventListener('click',()=>setMode('square'));
$('originalMode').addEventListener('click',()=>setMode('original'));
for(const which of ['start','end']) {
  $(`${which}Range`).addEventListener('input',()=>changeTime(which,number(`${which}Range`)));
  $(`${which}Time`).addEventListener('change',()=>changeTime(which,number(`${which}Time`)));
  $(`${which}Frame`).addEventListener('change',()=>changeFrame(which,number(`${which}Frame`)));
  $(`${which}Prev`).addEventListener('click',()=>changeFrame(which,state[which==='start'?'first':'last']-1));
  $(`${which}Next`).addEventListener('click',()=>changeFrame(which,state[which==='start'?'first':'last']+1));
  $(`view${which==='start'?'Start':'End'}`).addEventListener('click',()=>showFrame(state[which==='start'?'first':'last']));
  $(`mark${which==='start'?'Start':'End'}`).addEventListener('click',()=>changeTime(which,video.currentTime));
}
for(const id of ['cropOffset','cropRange']) $(id).addEventListener('input',()=>setOffset(number(id)));
$('alignStart').addEventListener('click',()=>setOffset(0));
$('alignCenter').addEventListener('click',()=>setOffset(maxOffset()/2));
$('alignEnd').addEventListener('click',()=>setOffset(maxOffset()));
$('speed').addEventListener('input',updateSummary);
document.querySelectorAll('[data-speed]').forEach(el=>el.addEventListener('click',()=>{$('speed').value=el.dataset.speed;updateSummary();}));
$('play').addEventListener('click',()=>{
  if(!video.paused) { video.pause(); return; }
  hideStill(); state.rangePlaying=false; video.playbackRate=1;
  video.play().catch(error=>notice(error.message));
});
$('playRange').addEventListener('click',()=>{
  const speed=number('speed');
  if(!Number.isFinite(speed)||speed<=0) { notice('請輸入有效播放速度。'); return; }
  hideStill(); video.currentTime=state.start; state.rangePlaying=true;
  try { video.playbackRate=speed; } catch(_) { state.rangePlaying=false; notice('瀏覽器不支援此預覽速度；仍可用該倍率製作貼圖。'); return; }
  video.play().catch(error=>notice(error.message));
});
video.addEventListener('play',()=>{$('play').textContent='Ⅱ 暫停';});
video.addEventListener('pause',()=>{$('play').textContent='▶ 播放';});
function updatePlayhead() {
  $('playTime').textContent=timeLabel(video.currentTime);
  if(state.meta) $('playhead').style.left=`${clamp(video.currentTime/state.meta.duration*100,0,100)}%`;
  if(state.rangePlaying&&video.currentTime>=state.end) { video.pause(); state.rangePlaying=false; }
}
video.addEventListener('timeupdate',updatePlayhead);
if(video.requestVideoFrameCallback) {
  const tick=()=>{updatePlayhead();video.requestVideoFrameCallback(tick);};video.requestVideoFrameCallback(tick);
}
video.addEventListener('error',()=>{
  if(state.id&&video.getAttribute('src')) {
    if(!state.fallback) fallbackPreview();
    else notice('瀏覽器無法播放預覽。你仍可使用原片影格預覽與製作貼圖。');
  }
});
$('fallback').addEventListener('click',fallbackPreview);
$('makeSticker').addEventListener('click',makeSticker);
new ResizeObserver(fitViewport).observe($('stage'));
setFrameControls(false); refresh();
