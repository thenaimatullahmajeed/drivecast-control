// Isolated UI fixtures. All mutations intercepted; no cloud resources or uploads.
const assert=require('assert'),fs=require('fs'),path=require('path');
const {chromium}=require(process.env.DRIVECAST_PLAYWRIGHT||'playwright');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const root=process.env.DRIVECAST_TEST_URL||'http://127.0.0.1:8877';
 const output=path.resolve(__dirname,'../../DriveCast-Azure-Review');
 fs.mkdirSync(output,{recursive:true});
 const channel='UC'+'a'.repeat(22),posts=[],errors=[];
 const state={version:2,workspace_id:'fixture',unlocked:true,vault_exists:true,last_sync:'',job:{busy:false},
  clients:{},connections:{yt:{id:'yt',kind:'youtube',label:'Nora Fans',channel_id:channel,status:'Connected'},
  drive:{id:'drive',kind:'drive',label:'Project Drive',status:'Connected'}},google_json_saved:true,
  drive_root:{folder_id:'root',connection_id:'drive',name:'Clients',folders:[{id:'f',name:'Nora Fans',path:'Nora Fans'}]},
  cloud:{hosted:true,live_uploads:false,version:'azure-v1',worker_capacity:2,reservations:7,uncertain:1,
   disk_free:32*1024**3,disk_total:64*1024**3,services:{worker:Date.now()/1000,scheduler:Date.now()/1000},
   trial_ends:'2026-11-20',trial_days_remaining:28,matching:[
    {name:'Nora Fans',status:'Matched',folder_id:'f',youtube_connection:'yt',channel_id:channel,ready:true,folders:[{id:'f',path:'Nora Fans'}],channels:[{channel_id:channel}]},
    {name:'Waiting creator',status:'Folder found, waiting for channel authorization',folders:[{path:'Waiting creator'}],channels:[],authorization_action:'connect-youtube'},
    {name:'No folder',status:'Channel found, no folder',folders:[],channels:[{channel_id:'other'}]},
    {name:'Duplicate',status:'Ambiguous name',folders:[{path:'A/Duplicate'},{path:'B/Duplicate'}],channels:[]}],
   routes:[],jobs:[],last_backup:{}}};
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async r=>{
  const request=r.request(),url=request.url();
  if(!url.startsWith(root+'/'))return r.abort();
  if(request.method()!=='GET'){
   posts.push({url,body:request.postDataJSON()});
   if(url.endsWith('/api/cloud/stage')){
    const b=request.postDataJSON();assert.equal(b.folder_id,'f');assert.equal(b.channel_id,channel);assert.equal(b.confirmed,true);
    return r.fulfill({status:202,json:{ok:true}});
   }
   if(url.endsWith('/api/cloud/server'))return r.fulfill({status:202,json:{ok:true}});
   throw Error('Unexpected mutation '+url);
  }
  if(url.endsWith('/api/state'))return r.fulfill({json:state});
  if(url.endsWith('/api/records'))return r.fulfill({json:{uploads:[],runs:[],activity:[]}});
  if(url.endsWith('/auth/session'))return r.fulfill({json:{signed_in:true}});
  return r.continue();
 });
 await page.goto(root+'/#matching');
 await page.getByRole('heading',{name:'Drive folders and authorized channels'}).waitFor();
 const text=await page.locator('#content').innerText();
 for(const label of ['Matched','Folder found, waiting for channel authorization','Channel found, no folder','Ambiguous name'])assert(text.includes(label));
 assert.equal(await page.getByRole('button',{name:'Start Public',exact:true}).count(),0);
 await page.screenshot({path:path.join(output,'matching-desktop.png'),fullPage:true});
 await page.getByRole('button',{name:'Review cloud preview',exact:true}).click();
 await page.locator('#cloudRouteForm').waitFor();
 assert.equal(await page.locator('#cloudRouteForm [name=youtube_connection]').inputValue(),'yt');
 assert((await page.locator('#modal').innerText()).includes('No upload starts until you approve the exact route'));
 await page.locator('#cloudRouteForm button').click();
 await page.waitForFunction(()=>!document.querySelector('#modal').open);
 assert.equal(posts.filter(p=>p.url.endsWith('/api/cloud/stage')).length,1);
 await page.goto(root+'/#server');await page.getByRole('heading',{name:'Service heartbeat'}).waitFor();
 assert((await page.locator('#content').innerText()).includes('Dry-run-only mode'));
 await page.screenshot({path:path.join(output,'server-desktop.png'),fullPage:true});
 await page.goto(root+'/#github');
 assert.equal(await page.locator('#navigation a[href="/#github"]').count(),0);assert.equal(await page.evaluate(()=>current),'dashboard');
 assert.equal(await page.locator('#content #remoteForm').count(),0);
 await page.goto(root+'/#backup');assert.equal(await page.locator('#remoteForm').count(),0);
 state.cloud.trial_days_remaining=null;state.cloud.trial_ends='';state.cloud.last_backup={at:new Date().toISOString(),off_azure_verified:true,targets:{drive:{verified:true},azure:{verified:true}},notice:'Encrypted independent backup verified.'};
 await page.reload();await page.waitForFunction(()=>S.cloud.last_backup?.off_azure_verified===true);
 await page.goto(root+'/#backup');await page.getByRole('heading',{name:'Server recovery outside Azure'}).waitFor();
 assert((await page.locator('#content').innerText()).includes('Private Drive verified'));
 assert(!(await page.locator('#content').innerText()).includes('Azure trial ends in'));
 await page.goto(root+'/#server');assert((await page.locator('#content').innerText()).includes('Paid hosting: no trial expiry is configured.'));
 await page.setViewportSize({width:390,height:844});
 await page.goto(root+'/#matching');await page.getByRole('heading',{name:'Drive folders and authorized channels'}).waitFor();
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
 await page.waitForFunction(()=>document.getElementById('sidebar').getBoundingClientRect().right<=0.1);
 await page.screenshot({path:path.join(output,'matching-mobile.png'),fullPage:true});
 assert.deepEqual(errors,[]);
 await browser.close();console.log('Cloud UI verified: matching states, exact-ID preview, dry-only controls, Azure-only navigation, mobile layout. All mutations intercepted.');
})().catch(e=>{console.error(e);process.exit(1)});
