const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const source=fs.readFileSync(path.resolve(__dirname,'../panel.js'),'utf8');
const context=vm.createContext({R:{uploads:[]}});
vm.runInContext(source.slice(source.indexOf('function isTestVideo('),source.indexOf('function videoRows(')),context);
const call=expression=>vm.runInContext(expression,context);
assert.equal(call("videoVisibility({status:'Dry run',actual_privacy:'unknown'}).key"),'test');
assert.equal(call("videoVisibility({status:'Dry run',actual_privacy:'unknown'}).label"),'Test only — no upload');
assert.equal(call("videoVisibility({status:'Failed',actual_privacy:'unknown'}).label"),'Upload not confirmed');
assert.equal(call("videoVisibility({status:'Done',youtube_id:'accepted',actual_privacy:'unknown'}).label"),'Visibility not checked');
assert.equal(call("videoVisibility({status:'Done',youtube_id:'accepted',actual_privacy:'private',requested_privacy:'public'}).key"),'private');
assert.equal(call("videoVisibility({status:'Done',youtube_id:'accepted',actual_privacy:'public'}).key"),'public');
// A newer dry run cannot hide an older accepted upload for the same source.
context.rows=[{client_id:'a',file_id:'x',status:'Dry run',recorded_at:'2026-10-21'},
 {client_id:'a',file_id:'x',status:'Done',youtube_id:'accepted',actual_privacy:'public',recorded_at:'2026-10-20'},
 {client_id:'b',file_id:'x',status:'Dry run'}];
assert.equal(call('uniqueVideos(rows).length'),2);assert.equal(call('uniqueVideos(rows)[0].youtube_id'),'accepted');
context.rows=[{client_id:'a',file_id:'x',status:'Dry run'},{client_id:'a',file_id:'x',status:'Needs review'}];
assert.equal(call('uniqueVideos(rows)[0].status'),'Needs review');
console.log('Video record display passed: test-only, unverified accepted, uncertain, private/public and accepted-upload deduplication.');
