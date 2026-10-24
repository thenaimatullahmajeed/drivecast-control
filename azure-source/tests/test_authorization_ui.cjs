// All POSTs and provider navigation are intercepted. No real consent or worker writes.
const assert=require('assert');
const {chromium}=require(process.env.DRIVECAST_PLAYWRIGHT||'playwright');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();
 const root=process.env.DRIVECAST_TEST_URL||'http://127.0.0.1:8765';
 let posts=[],errors=[],mode='ready',job={busy:false},channelId,connectionId;
 page.on('pageerror',error=>errors.push(error.message));
 await page.route('**/*',async route=>{
  const request=route.request(),url=request.url();
  if(!url.startsWith(root+'/'))return route.abort();
  if(request.method()!=='GET'){
   assert(url.endsWith('/api/google'),'Unexpected mutation: '+url);
   posts.push(request.postDataJSON());
   if(mode==='reject')return route.fulfill({status:400,json:{error:'Another operation is finishing. Please wait.'}});
   job={busy:true,label:'Connect youtube',outcome:'running',notice:'Waiting for Google sign-in',auth_url:'https://accounts.google.com/o/oauth2/v2/auth?client_id=fixture-only'};
   return route.fulfill({status:202,json:{ok:true}});
  }
  if(url.endsWith('/api/state')){
   const response=await route.fetch(),state=await response.json();state.job={...state.job,...job};
   state.cloud={hosted:false};
   state.connections={
    yt:{id:'yt',kind:'youtube',label:'Fixture channel',channel_id:'UC'+'a'.repeat(22),status:'Connected'},
    gh:{id:'gh',kind:'github',login:'fixture-owner',label:'fixture-owner',status:'Connected'}};
   state.clients={fixture:{id:'fixture',name:'Fixture channel',channel_title:'Fixture channel',youtube_connection:'yt',channel_id:state.connections.yt.channel_id,github_connection:'gh',deployed:true,enabled:false,source_type:'drive'}};
   const yt=Object.values(state.connections).find(c=>c.kind==='youtube');connectionId=yt.id;channelId=yt.channel_id;
   state.google_json_saved=true;state.unlocked=true;
   const gh=Object.values(state.connections).find(c=>c.kind==='github');
   Object.assign(gh,{status:'Account suspended',notice:'GitHub account suspended. Request an account review.',health:{issue:'suspended',token_valid:false}});
   const client=Object.values(state.clients).find(c=>c.github_connection===gh.id);client.notice='GitHub account suspended.';
   return route.fulfill({response,json:state});
  }
  return route.continue();
 });
 await page.goto(root+'/#youtube');await page.waitForFunction(()=>S.google_json_saved);
 await page.locator('[data-action=connect-youtube][data-id]').first().click();
 // Regression: <input name=id> shadows HTMLFormElement.id. Dispatch must use getAttribute.
 assert.equal(await page.locator('#googleConnectForm').evaluate(f=>typeof f.id),'object');
 await page.getByRole('button',{name:'Create authorization link',exact:true}).click();
 await page.locator('#googleConnectForm').getByRole('link',{name:'Open Google sign-in ↗'}).waitFor();
 assert.equal(posts.length,1);assert.equal(posts[0].id,connectionId);assert.equal(posts[0].expected_channel_id,channelId);
 assert.equal(posts[0].kind,'youtube');assert.equal(posts[0].require_exact_name,true);
 assert(await page.getByRole('button',{name:'Create authorization link',exact:true}).isDisabled());
 assert.equal(await page.locator('#googleConnectForm [data-action=copy-auth]').count(),1);
 job={busy:false,outcome:'error',notice:'Google sign-in timed out. Create a new link.'};
 await page.evaluate(()=>refresh());
 assert((await page.locator('#googleConnectForm .form-feedback').innerText()).includes('timed out'));
 assert(await page.getByRole('button',{name:'Create authorization link',exact:true}).isEnabled());
 mode='reject';
 await page.getByRole('button',{name:'Create authorization link',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('#googleConnectForm .form-feedback').textContent.includes('Another operation'));
 assert(await page.getByRole('button',{name:'Create authorization link',exact:true}).isEnabled());
 await page.getByRole('button',{name:'Close dialog',exact:true}).click();
 await page.goto(root+'/#github');await page.waitForFunction(()=>current==='dashboard');
 assert.equal(await page.locator('#navigation a[href="/#github"]').count(),0);
 await page.setViewportSize({width:390,height:844});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
 assert.deepEqual(errors,[]);
 await browser.close();
 console.log('Authorization UI passed: real click with hidden id, correct channel payload, visible sign-in link, timeout and rejected-request recovery, removed GitHub navigation, zero real POSTs.');
})().catch(error=>{console.error(error);process.exit(1)});
