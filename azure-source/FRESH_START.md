# Fresh Azure workspace

DriveCast ka active flow ab **Google Drive → Azure → YouTube** hai. GitHub page, account filter, worker assignment aur Move-to-GitHub buttons removed hain. Purane infrastructure endpoints panel se disabled hain; offline legacy adapter sirf archived history/recovery ke liye source mein retained hai.

## Seedha setup

1. Google Drive mein account authorize karke main project folder choose karein.
2. YouTube mein har intended channel ko dobara authorize karein. Google sign-in email aur exact returned channel ID check karein.
3. Folder matching mein matching subfolder choose karein, dry run karein aur exact channel setup confirm karein.
4. Publishing default off hai. Pehle approved test aur phir go-live karein. Purani manually uploaded videos apne source queue se review karein.

Hosted authorizations ke liye Google OAuth client **Web application** hona chahiye; YouTube page par displayed HTTPS callback Google Console mein register karein. Google API app settings reset mein retained rehti hain; old Desktop app ko Web app se replace karna par sakta hai.

## Delete aur reset

Channels page par **Delete** ya **Delete selected** se channel ki saved authorization, route, local video records aur stats remove hote hain. YouTube page par route ke baghair bhi authorized channel delete kar sakte hain. Confirmation mein `DELETE` type karein.

Backup → Fresh start → Reset channel workspace mein `RESET` type karne se sab channel data, platform connections aur folder assignments clear hote hain. Login account, Google API app configuration aur Telegram settings retained hain. Reset history ki jagah fresh workspace identity banata hai, taake stale processes old data wapas save na kar saken.

Cleanup se pehle encrypted checkpoint `recovery-backups` mein banta hai. Backup failure par deletion rukti hai. Active worker check ke darmiyan deletion blocked hai; pehle pause aur check finish hone ka wait karein. Source files ya published YouTube videos delete nahi hote. Old remote systems is button se automatically modify nahi hote.

Fresh history purani uploads ko identify nahi kar sakti. Tool copied files ya manual uploads ko title se reliably deduplicate nahi karta. Reauthorization se posting start nahi hoti, aur uncertain new uploads normal at-most-once protection se quarantined rehte hain.

Encrypted recovery copies jaan-boojh kar retain hoti hain. Unko aur unki separate keys ko private rakhein. Yeh logical workspace reset hai; old backups ki secure erasure is operation ka hissa nahi.


## Live posting controls

Dashboard, Channels aur route previews par **Post force now** ek nayi video Public post karne ka immediate check queue karta hai. Daily limit, exact channel verification, quarantined outcomes aur duplicate reservations is mode mein bhi enforce hote hain. Button paused channel ke liye one-off post kar sakta hai; regular schedule apne aap resume nahi hota.

Automatic checks five minutes apart hain; har check ek video try karta hai. Countdown **next Drive/video check** ka hai, guaranteed upload completion ka nahi. Next filename last verified queue snapshot se aata hai. Active upload, no videos aur daily cap ke states alag dikhte hain; cap next Pacific midnight par reset hota hai.

Automation mein **Start Public after confirmed setup** per-workspace preference hai. Is se authorization alone post nahi karta: exact folder/channel preview, successful dry run aur setup confirmation phir bhi required hain. Hosting-level live gate bhi approved on hona chahiye.

YouTube → Refresh photos Google/YouTube ki current thumbnail ko force-refresh karta hai. Agar provider default initial icon deta hai to wohi photo dikhegi; tool custom channel picture invent nahi karta.
