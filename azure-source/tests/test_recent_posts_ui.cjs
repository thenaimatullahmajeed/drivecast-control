// Deterministic UI regression. Every API request is intercepted; no real posting.
const assert=require('assert'),fs=require('fs'),path=require('path'),{chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const p=await browser.newPage({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
 const root=process.env.DRIVECAST_TEST_URL||'http://127.0.0.1:8877',errors=[],writes=[],now=Date.now();
 const clients=Object.fromEntries(['a','b','c'].map((id,i)=>[id,{id,name:['Nora Rose Jean Fans','Jess Bacon Fans','Michelle juliet Nayla Fans'][i],channel_title:['Nora Rose Jean Fans','Jess Bacon Fans','Michelle juliet Nayla Fans'][i],channel_id:'UC'+id.repeat(22),youtube_connection:'yt-'+id,enabled:true,deployed:true,folder_path:'Clients / '+id,cloud_route_id:id,next_schedule:{next_at:new Date(now+300000+i*60000).toISOString(),label:'Next Drive check',next_video:{name:'Next clip '+id+'.mp4'}}}]));
 const records=[];
 for(let i=0;i<23;i++)records.push({client_id:'a',client_name:clients.a.name,file_id:'a-file-'+i,youtube_id:'a'+String(i).padStart(10,'0'),title:i?'Nora archive '+i:'Nora newest <script>alert(1)</script>',status:'Done',recorded_at:new Date(now-60000-i*3600000).toISOString(),actual_privacy:'public',youtube_views:i?i:1234,youtube_likes:0,youtube_comments:7,stats_checked_at:new Date(now-10000).toISOString()});
 records.push({client_id:'b',client_name:clients.b.name,file_id:'b-file',youtube_id:'bbbbbbbbbbb',title:'Jess newest upload',status:'Done',recorded_at:new Date(now-30000).toISOString(),actual_privacy:'public',youtube_views:900,youtube_likes:42,youtube_comments:3,stats_checked_at:new Date(now-5000).toISOString()},
 {client_id:'b',client_name:clients.b.name,file_id:'b-old',youtube_id:'b0000000000',title:'Jess older upload',status:'Done',recorded_at:new Date(now-990000).toISOString(),actual_privacy:'public',youtube_views:100,youtube_likes:1,youtube_comments:0},
 {client_id:'c',client_name:clients.c.name,file_id:'c-file',youtube_id:'ccccccccccc',title:'Michelle private upload',status:'Done',recorded_at:new Date(now-80000).toISOString(),actual_privacy:'private',youtube_views:null,youtube_likes:null,youtube_comments:null},
 {client_id:'a',file_id:'dry',youtube_id:'',title:'DRY RUN MUST NOT APPEAR',status:'Dry run',recorded_at:new Date(now+9999).toISOString()},
 {client_id:'a',file_id:'uncertain',youtube_id:'',title:'UNCERTAIN MUST NOT APPEAR',status:'Uncertain',recorded_at:new Date(now+9999).toISOString()});
 records.push({...records[0],run_id:'duplicate'});records.reverse();
 const state={workspace_id:'fixture',unlocked:true,vault_exists:true,job:{busy:false},last_sync:new Date(now-5000).toISOString(),google_json_saved:true,google_app_ready:true,clients,connections:Object.fromEntries(Object.values(clients).map(c=>[c.youtube_connection,{id:c.youtube_connection,kind:'youtube',label:c.name,channel_id:c.channel_id,status:'Connected'}])),cloud:{hosted:true,live_uploads:true,workers:[],routes:Object.values(clients).map(c=>({id:c.id,history_verified:1,config:{...c,max_uploads_per_day:2},plan:{daily_remaining:2}})),matching:[]}};
 p.on('pageerror',e=>errors.push(e.message));
 await p.route('**/*',async route=>{
  const req=route.request(),url=req.url();if(!url.startsWith(root+'/'))return route.abort();
  if(req.method()!=='GET'){writes.push(url);throw Error('No mutation expected: '+url);}
  if(url.endsWith('/api/state'))return route.fulfill({json:state});
  if(url.endsWith('/api/records'))return route.fulfill({json:{uploads:records,runs:[],activity:[]}});
  if(url.endsWith('/auth/session'))return route.fulfill({json:{signed_in:true}});
  return route.continue();
 });
 const out=path.resolve(__dirname,'../../DriveCast-Compact-UI');fs.mkdirSync(out,{recursive:true});
 await p.goto(root+'/#dashboard');await p.locator('#recent-posts').waitFor();
 assert.equal(await p.locator('#content > :first-child').getAttribute('class'),'kpis dashboard-kpis');
 assert.equal(await p.locator('#recent-posts tbody tr').count(),3);
 assert.equal(await p.locator('#recent-posts tbody tr').first().getAttribute('data-video-id'),'bbbbbbbbbbb');
 const dashboardText=await p.locator('#content').innerText();
 assert(!dashboardText.includes('DRY RUN MUST NOT APPEAR')&&!dashboardText.includes('UNCERTAIN MUST NOT APPEAR'));
 assert(!await p.locator('#recent-posts script').count());
 const compact=await p.evaluate(()=>({font:getComputedStyle(document.documentElement).fontSize,padding:getComputedStyle(document.querySelector('.recent-card')).paddingTop,recent:document.querySelector('#recent-posts').getBoundingClientRect().top,setup:[...document.querySelectorAll('h2')].find(h=>h.textContent==='Your setup at a glance').getBoundingClientRect().top,kpis:document.querySelector('.kpis').getBoundingClientRect().bottom}));
 assert.equal(compact.font,'14px');assert.equal(compact.padding,'16px');assert(compact.recent>=compact.kpis&&compact.setup>compact.recent);
 const nora=p.locator('#recent-posts tr[data-video-id="a0000000000"]');
 assert((await nora.innerText()).includes('1,234'));assert.equal((await nora.locator('.recent-number').nth(1).innerText()).trim(),'0');
 const privateRow=p.locator('#recent-posts tr[data-video-id="ccccccccccc"]');assert((await privateRow.innerText()).includes('Private'));assert.equal((await privateRow.locator('.recent-number').first().innerText()).trim(),'—');
 await p.screenshot({path:path.join(out,'dashboard-desktop.png'),fullPage:true});
 await p.locator('#recent-posts').getByRole('link',{name:'View all →',exact:true}).click();await p.locator('#recentMode').waitFor();
 assert.equal(await p.locator('.recent-table tbody tr').count(),20);assert((await p.locator('.paging').innerText()).includes('1 of 2'));
 await p.getByRole('button',{name:'Next',exact:true}).click();assert.equal(await p.locator('.recent-table tbody tr').count(),6);
 await p.locator('#recentMode').selectOption('latest');assert.equal(await p.locator('.recent-table tbody tr').count(),3);
 await p.locator('#recentSearch').fill('Nora archive');await p.waitForTimeout(250);assert.equal(await p.locator('.recent-table tbody tr').count(),0);
 await p.getByRole('button',{name:'Clear',exact:true}).click();
 await p.locator('#recentClient').selectOption('b');assert.equal(await p.locator('.recent-table tbody tr').count(),2);
 await p.locator('#recentSearch').fill('Jess newest');await p.waitForTimeout(250);assert.equal(await p.locator('.recent-table tbody tr').count(),1);
 const video=await p.locator('.recent-title').getAttribute('href');assert.equal(video,'https://youtu.be/bbbbbbbbbbb');assert.equal(await p.locator('.recent-title').getAttribute('target'),'_blank');
 await p.getByRole('button',{name:'Clear',exact:true}).click();await p.screenshot({path:path.join(out,'recent-desktop.png'),fullPage:true});
 records.push({client_id:'c',client_name:clients.c.name,file_id:'c-new',youtube_id:'c1111111111',title:'Just accepted automatically',status:'Done',actual_privacy:'public',recorded_at:new Date(now+1000).toISOString(),youtube_views:0,youtube_likes:0,youtube_comments:0});
 await p.evaluate(()=>refresh(true));assert.equal(await p.locator('.recent-table tbody tr').first().getAttribute('data-video-id'),'c1111111111');
 await p.goto(root+'/#dashboard');assert.equal(await p.locator('#recent-posts tbody tr').count(),3);assert.equal(await p.locator('#recent-posts tbody tr').first().getAttribute('data-video-id'),'c1111111111');
 for(const width of [1024,768,390]){await p.setViewportSize({width,height:844});await p.goto(root+'/#dashboard');assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'Dashboard overflow at '+width);await p.goto(root+'/#recent');assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'Recent overflow at '+width);}
 await p.screenshot({path:path.join(out,'recent-mobile.png'),fullPage:true});await p.goto(root+'/#dashboard');await p.screenshot({path:path.join(out,'dashboard-mobile.png'),fullPage:true});
 await p.setViewportSize({width:1440,height:1000});await p.goto(root+'/#channels');assert.equal(await p.locator('#heading').innerText(),'Channels');await p.screenshot({path:path.join(out,'channels-desktop.png'),fullPage:true});
 assert.deepEqual(writes,[]);assert.deepEqual(errors,[]);await browser.close();
 console.log('Recent UI passed: stats-first density, latest per channel, accepted-only chronological dedup, live refresh, real visibility, missing-vs-zero counters, search/channel/mode filters, pagination, safe links, mobile layout. No publishing.');
})().catch(e=>{console.error(e);process.exit(1)});
