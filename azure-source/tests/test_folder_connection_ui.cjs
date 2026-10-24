// Missing-folder regression. All provider/mutation requests are intercepted.
const assert=require('assert'),fs=require('fs'),path=require('path'),{chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
 const root=process.env.DRIVECAST_TEST_URL||'http://127.0.0.1:8877',posts=[],errors=[];
 const yt={id:'michelle',kind:'youtube',label:'Michelle juliet Nayla Fans',channel_id:'UC'+'m'.repeat(22),status:'Connected'};
 const state={workspace_id:'fixture',unlocked:true,vault_exists:true,google_json_saved:true,clients:{},job:{busy:false},connections:{michelle:yt,other:{...yt,id:'other',label:'Other channel',channel_id:'UC'+'o'.repeat(22)},drive:{id:'drive',kind:'drive',label:'Project Drive',status:'Connected'}},drive_root:{folder_id:'root',connection_id:'drive',name:'Main',checked_at:new Date().toISOString(),folders:[]},cloud:{hosted:true,live_uploads:true,routes:[],jobs:[],matching:[{name:yt.label,status:'Channel found, no folder',youtube_connection:yt.id,channel_id:yt.channel_id,folders:[],channels:[{id:yt.id,channel_id:yt.channel_id}],ready:false}]}};
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{
  const req=route.request(),url=req.url();if(!url.startsWith(root+'/'))return route.abort();
  if(req.method()!=='GET'){
   const body=req.postDataJSON();posts.push({url,body});
   if(url.endsWith('/api/drive/discover')){
    state.drive_root.folders=[{id:'michelle-folder',name:yt.label,path:yt.label}];
    state.cloud.matching[0]={...state.cloud.matching[0],status:'Matched',ready:true,folder_id:'michelle-folder',folders:state.drive_root.folders};
   }else if(url.endsWith('/api/cloud/stage')){
    assert.deepEqual(body,{folder_id:'michelle-folder',youtube_connection:yt.id,max_uploads_per_day:'3',channel_id:yt.channel_id,confirmed:true});
   }else throw Error('Unexpected mutation '+url);
   state.job={busy:false,outcome:'success',result:{updated:true}};return route.fulfill({status:202,json:{ok:true}});
  }
  if(url.endsWith('/api/state'))return route.fulfill({json:state});
  if(url.endsWith('/api/records'))return route.fulfill({json:{uploads:[],runs:[],activity:[]}});
  if(url.endsWith('/auth/session'))return route.fulfill({json:{signed_in:true}});
  return route.continue();
 });
 const out=path.resolve(__dirname,'../../DriveCast-Folder-Connection-Fix');fs.mkdirSync(out,{recursive:true});
 await page.goto(root+'/#matching');await page.getByRole('button',{name:'Connect Drive folder',exact:true}).click();
 assert.equal(await page.locator('#cloudRouteForm [name=youtube_connection]').inputValue(),yt.id);
 assert(await page.getByRole('button',{name:'Confirm preview · no uploads',exact:true}).isDisabled());
 assert((await page.locator('#modal').innerText()).includes('inside your connected main folder'));
 await page.locator('#cloudRouteForm [name=max_uploads_per_day]').fill('3');
 await page.locator('#modal').getByRole('button',{name:'Refresh Drive folders',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('#cloudRouteForm [name=folder_id] option[value="michelle-folder"]'));
 assert.equal(await page.locator('#cloudRouteForm [name=youtube_connection]').inputValue(),yt.id);
 assert.equal(await page.locator('#cloudRouteForm [name=max_uploads_per_day]').inputValue(),'3');
 assert.equal(await page.locator('#cloudRouteForm [name=folder_id]').inputValue(),'');
 assert.equal(await page.locator('#cloudRouteForm [name=folder_id]').getAttribute('required'),'');
 await page.locator('#cloudRouteForm [name=folder_id]').selectOption('michelle-folder');
 await page.screenshot({path:path.join(out,'folder-dialog-fixture-desktop.png'),fullPage:true});
 await page.getByRole('button',{name:'Confirm preview · no uploads',exact:true}).click();
 await page.waitForFunction(()=>!document.getElementById('modal').open);
 assert.equal(posts.filter(x=>x.url.endsWith('/api/cloud/stage')).length,1);
 await page.goto(root+'/#youtube');await page.locator('[data-action=route-youtube][data-id=michelle]').click();
 assert.equal(await page.locator('#cloudRouteForm [name=youtube_connection]').inputValue(),yt.id);
 await page.locator('#cloudRouteForm [name=folder_id]').selectOption('michelle-folder');
 await page.setViewportSize({width:390,height:844});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
 await page.screenshot({path:path.join(out,'folder-dialog-fixture-mobile.png'),fullPage:true});
 assert.deepEqual(errors,[]);await browser.close();
 console.log('Folder connection UI passed: missing-folder CTA, exact channel preselection, inline discovery, preserved selection/limit, required folder, no automatic publishing, YouTube shortcut and mobile.');
})().catch(e=>{console.error(e);process.exit(1)});
