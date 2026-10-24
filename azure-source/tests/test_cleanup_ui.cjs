// All mutations intercepted. No real account/channel data is deleted by this test.
const assert=require('assert'),fs=require('fs'),path=require('path');
const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
 const root=process.env.DRIVECAST_TEST_URL||'http://127.0.0.1:8877',posts=[],errors=[];
 const state={version:2,workspace_id:'fixture',unlocked:true,vault_exists:true,job:{busy:false},clients:{},connections:{},
  cloud:{hosted:true,live_uploads:false,routes:[],jobs:[],matching:[],workers:[],services:{}},google_json_saved:true};
 for(let i=1;i<=2;i++){
  state.connections['yt'+i]={id:'yt'+i,kind:'youtube',label:'Fixture '+i,channel_id:'UC'+String(i).repeat(22),status:'Connected'};
  state.clients['c'+i]={id:'c'+i,name:'Fixture '+i,channel_id:state.connections['yt'+i].channel_id,youtube_connection:'yt'+i,runner:'azure',cloud_route_id:'r'+i,folder_path:'Folder '+i,enabled:false,deployed:true};
 }
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async r=>{
  const q=r.request(),url=q.url();if(!url.startsWith(root+'/'))return r.abort();
  if(q.method()!=='GET'){
   assert(url.endsWith('/api/workspace/cleanup'));const body=q.postDataJSON();posts.push(body);assert.equal(body.confirmed,true);
   if(body.reset){assert.equal(body.phrase,'RESET');state.clients={};state.connections={};}
   else for(const id of body.ids){const client=state.clients[id];if(client)delete state.connections[client.youtube_connection];delete state.clients[id];}
   state.job={busy:false,outcome:'success',result:{updated:true}};return r.fulfill({status:202,json:{ok:true}});
  }
  if(url.endsWith('/api/state'))return r.fulfill({json:state});
  if(url.endsWith('/api/records'))return r.fulfill({json:{uploads:[],runs:[],activity:[]}});
  if(url.endsWith('/auth/session'))return r.fulfill({json:{signed_in:true}});
  return r.continue();
 });
 await page.goto(root+'/#channels');await page.locator('[data-action=delete-channel][data-id=c1]').waitFor();
 assert(!/GitHub|Move to/.test(await page.locator('body').innerText()));
 await page.locator('[data-action=delete-channel][data-id=c1]').click();
 assert((await page.locator('#modalBody').innerText()).includes('YouTube videos and Drive files stay'));
 await page.locator('#cleanupForm [name=phrase]').fill('DELETE');
 await page.locator('#cleanupForm button').click();await page.waitForFunction(()=>!S.clients.c1);
 assert.deepEqual(posts[0].ids,['c1']);assert(state.clients.c2);
 await page.goto(root+'/#youtube');await page.locator('[data-action=delete-channel][data-id=yt2]').waitFor();
 assert(!/GitHub/.test(await page.locator('#content').innerText()));
 await page.goto(root+'/#channels');await page.locator('[data-select=c2]').check();
 await page.getByRole('button',{name:'Delete selected',exact:true}).click();
 await page.locator('#cleanupForm [name=phrase]').fill('DELETE');await page.locator('#cleanupForm button').click();
 await page.waitForFunction(()=>Object.keys(S.clients).length===0);assert.deepEqual(posts[1].ids,['c2']);
 state.connections.extra={id:'extra',kind:'youtube',label:'Remaining authorization',channel_id:'UC'+'a'.repeat(22)};
 await page.goto(root+'/#backup');await page.getByRole('button',{name:'Reset channel workspace',exact:true}).click();
 await page.locator('#cleanupForm [name=phrase]').fill('RESET');await page.locator('#cleanupForm button').click();
 await page.waitForFunction(()=>Object.keys(S.connections).length===0);assert.equal(posts[2].reset,true);
 await page.goto(root+'/#dashboard');await page.getByRole('heading',{name:'Let’s connect your first channel'}).waitFor();
 const output=path.resolve(__dirname,'../../DriveCast-Fresh-Start');fs.mkdirSync(output,{recursive:true});
 await page.screenshot({path:path.join(output,'fresh-dashboard-desktop.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});
 await page.waitForFunction(()=>document.getElementById('sidebar').getBoundingClientRect().right<=1);
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
 await page.screenshot({path:path.join(output,'fresh-dashboard-mobile.png'),fullPage:true});
 state.google_app_ready=false;
 await page.goto(root+'/#youtube');await page.evaluate(()=>refresh(true));
 await page.locator('#headerActions [data-action=connect-youtube]').click();
 await page.getByRole('heading',{name:'One-time Google Web app setup'}).waitFor();
 assert.equal(posts.length,3);await page.getByRole('button',{name:'Close dialog',exact:true}).click();
 await page.goto(root+'/#github');await page.waitForFunction(()=>current==='dashboard');
 assert.deepEqual(errors,[]);await browser.close();
 console.log('Cleanup UI passed: per-channel deletion, bulk deletion, authorization deletion controls, typed reset, Azure-only navigation and fresh mobile onboarding. All writes intercepted.');
})().catch(e=>{console.error(e);process.exit(1)});
