// Deterministic browser fixtures; all upload/actions are intercepted.
const assert=require('assert'),fs=require('fs'),path=require('path'),{chromium}=require('playwright');
(async()=>{
 const b=await chromium.launch({channel:'msedge',headless:true});
 const p=await b.newPage({viewport:{width:1440,height:1050},reducedMotion:'reduce'}),root=process.env.DRIVECAST_TEST_URL||'http://127.0.0.1:8877',errors=[],posts=[];
 const avatar='/avatars/'+'a'.repeat(64)+'.img',plan={kind:'scheduled',label:'Next video check',next_at:new Date(Date.now()+240000).toISOString(),next_video:{id:'file',name:'Upcoming clip.mp4'},daily_remaining:2};
 const client={id:'r',name:'Fixture channel',channel_title:'Fixture channel',channel_id:'UC'+'a'.repeat(22),youtube_connection:'yt',cloud_route_id:'r',folder_path:'Channel folder',runner:'azure',enabled:true,deployed:true,next_schedule:plan,next_video:plan.next_video};
 const s={workspace_id:'fixture',unlocked:true,vault_exists:true,job:{busy:false},google_json_saved:true,connections:{yt:{id:'yt',kind:'youtube',label:'Fixture channel',avatar,channel_id:client.channel_id,status:'Connected'}},clients:{r:client},cloud:{hosted:true,live_uploads:true,auto_start_confirmed:true,workers:[],jobs:[],matching:[],routes:[{id:'r',config:{...client,max_uploads_per_day:2},history_verified:1,enabled:1,plan}]}};
 p.on('pageerror',e=>errors.push(e.message));
 await p.route('**/*',async r=>{
  const q=r.request(),u=q.url();if(!u.startsWith(root+'/'))return r.abort();
  if(q.method()!=='GET'){
   const body=q.postDataJSON();posts.push({url:u,body});
   if(u.endsWith('/api/cloud/action')){assert.deepEqual(body,{id:'r',action:'post_now',confirmed:true});plan.kind='active';plan.label='Posting now';plan.next_at='';}
   else if(u.endsWith('/api/cloud/settings'))assert.deepEqual(body,{auto_start_confirmed:false});
   else if(u.endsWith('/api/avatars/refresh')){}
   else throw Error('Unexpected mutation '+u);
   s.job={busy:false,outcome:'success',result:{notice:'One new Public video queued.'}};return r.fulfill({status:202,json:{ok:true}});
  }
  if(u.endsWith('/api/state'))return r.fulfill({json:s});
  if(u.endsWith('/api/records'))return r.fulfill({json:{uploads:[],runs:[],activity:[]}});
  if(u.endsWith('/auth/session'))return r.fulfill({json:{signed_in:true}});
  if(u.endsWith('.img'))return r.fulfill({contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jD1sAAAAASUVORK5CYII=','base64')});
  return r.continue();
 });
 await p.goto(root+'/#dashboard');await p.getByRole('heading',{name:'Post force now',exact:true}).waitFor();
 await p.waitForFunction(()=>document.querySelector('img')?.naturalWidth>0);
 assert((await p.locator('#content').innerText()).includes('Upcoming clip.mp4'));
 assert((await p.locator('.countdown').first().innerText()).includes('m'));
 const output=path.resolve(__dirname,'../../DriveCast-Live-Controls');fs.mkdirSync(output,{recursive:true});
 await p.screenshot({path:path.join(output,'dashboard-fixture-desktop.png'),fullPage:true});
 await p.getByRole('button',{name:'Post force now',exact:true}).first().click();
 await p.waitForFunction(()=>S.cloud.routes[0].plan.kind==='active');
 assert(await p.getByRole('button',{name:'Post force now',exact:true}).first().isDisabled());assert.equal(posts.length,1);
 await p.goto(root+'/#channels');assert.equal(await p.getByRole('columnheader',{name:'Next check'}).count(),1);
 assert((await p.locator('#content').innerText()).includes('Posting now'));
 plan.kind='scheduled';plan.next_at=new Date(Date.now()+240000).toISOString();plan.label='Next video check';
 await p.goto(root+'/#youtube');await p.getByRole('button',{name:'Refresh photos',exact:true}).click();
 assert(posts.at(-1).url.endsWith('/api/avatars/refresh'));
 await p.goto(root+'/#automation');await p.locator('[name=auto_start_confirmed]').uncheck();await p.getByRole('button',{name:'Save posting preference',exact:true}).click();
 await p.setViewportSize({width:390,height:844});await p.goto(root+'/#dashboard');
 assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
 await p.screenshot({path:path.join(output,'dashboard-fixture-mobile.png'),fullPage:true});
 assert.deepEqual(errors,[]);await b.close();
 console.log('Live UI passed: actual image element, next filename/countdown, direct Force now payload, active-job disabling, channel counter, photo refresh, per-workspace opt-in and mobile. No real publishing.');
})().catch(e=>{console.error(e);process.exit(1)});
