const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

async function submit(...responses) {
  const template = fs.readFileSync(path.join(__dirname, '../attendance/web/templates/settings.html'), 'utf8');
  const script = template.slice(template.indexOf('  const textForm ='), template.indexOf('  const creationForm ='));
  let handler;
  const elements = {
    'voice-text-test': {action:'/settings/greeting/preview-text',addEventListener:(event,fn)=>{handler=fn;}},
    'voice-text-button': {disabled:false}, 'voice-text-status': {textContent:''}, 'voice-text-audio': {hidden:true}
  };
  const data = new Map();
  vm.runInNewContext(script, {
    document:{getElementById:id=>elements[id],querySelector:()=>({value:'maichi'})},
    window:{addEventListener:()=>{}}, FormData:class {set(key,value){data.set(key,value);}},
    fetch:async()=>{ assert.ok(responses.length, 'unexpected request'); return responses.shift(); },
    setTimeout:resolve=>resolve(), URL:{createObjectURL:()=> 'blob:test',revokeObjectURL:()=>{}}
  });
  await handler({preventDefault:()=>{}});
  assert.equal(elements['voice-text-button'].disabled, false);
  return elements;
}

test('proxy HTML errors show the HTTP status instead of hiding it', async()=>{
  const e=await submit({ok:false,status:504,statusText:'Gateway Timeout',json:async()=>{throw new SyntaxError();}});
  assert.match(e['voice-text-status'].textContent,/HTTP 504 Gateway Timeout/);
  assert.equal(e['voice-text-audio'].hidden,true);
});

test('application JSON errors preserve the actual cause', async()=>{
  const e=await submit({ok:false,status:500,json:async()=>({message:'Missing libsndfile'})});
  assert.match(e['voice-text-status'].textContent,/HTTP 500.*Missing libsndfile/);
});

test('a successful HTML response is rejected instead of being played as audio', async()=>{
  const e=await submit({ok:true,status:200,headers:{get:()=> 'text/html; charset=utf-8'},blob:()=>assert.fail('must reject HTML')});
  assert.match(e['voice-text-status'].textContent,/Expected WAV audio.*text\/html/);
  assert.equal(e['voice-text-audio'].hidden,true);
});

test('WAV responses still reach the audio player', async()=>{
  const e=await submit({ok:true,status:200,headers:{get:()=> 'audio/wav'},blob:async()=>({})});
  assert.equal(e['voice-text-audio'].hidden,false);
  assert.equal(e['voice-text-audio'].src,'blob:test');
});


test('queued camera previews are polled until WAV is ready', async()=>{
  const pending = {ok:true,status:202,json:async()=>({preview_url:'/settings/greeting/preview-text/token'})};
  const e=await submit(pending,pending,{ok:true,status:200,headers:{get:()=> 'audio/wav'},blob:async()=>({})});
  assert.equal(e['voice-text-audio'].src,'blob:test');
  assert.equal(e['voice-text-audio'].hidden,false);
});

test('camera polling failures show their HTTP status and cause', async()=>{
  const e=await submit({ok:true,status:202,json:async()=>({preview_url:'/result'})},
    {ok:false,status:502,statusText:'Bad Gateway',json:async()=>({message:'Camera: encoder failed'})});
  assert.match(e['voice-text-status'].textContent,/HTTP 502.*Camera: encoder failed/);
});
