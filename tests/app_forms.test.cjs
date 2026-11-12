const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function forms() {
  const handlers = {}, timers = [], classes = new Set();
  const button = {disabled:false, hasAttribute:()=>false, classList:{add:x=>classes.add(x),remove:x=>classes.delete(x)}};
  const document = {body:{dataset:{}}, documentElement:{dataset:{}}, querySelector:()=>null, querySelectorAll:()=>[],
    addEventListener:(name,fn)=>{handlers[name]=fn;}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../attendance/web/static/app.js'),'utf8'),
    {document,window:{},setTimeout:fn=>timers.push(fn)});
  return {submit:defaultPrevented=>handlers.submit({defaultPrevented,target:{dataset:{}},submitter:button}),button,classes,timers};
}

test('a form handled in place keeps control of its own loading state', () => {
  const f=forms(); f.submit(true);
  assert.equal(f.classes.size,0);
  assert.equal(f.timers.length,0);
  assert.equal(f.button.disabled,false);
});

test('normal form submission still gets its loading indicator and timeout reset', () => {
  const f=forms(); f.submit(false);
  assert(f.classes.has('is-loading'));
  f.timers[0](); assert.equal(f.button.disabled,true);
  f.timers[1](); assert.equal(f.button.disabled,false);
  assert.equal(f.classes.size,0);
});
