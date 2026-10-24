"""Cache only HTTPS provider images. No provider credentials/URLs in public state."""
import hashlib
import ipaddress
import socket
import time
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

DOMAINS=('googleusercontent.com','ggpht.com','githubusercontent.com')


def allowed(url):
    parsed=urlparse(url)
    host=(parsed.hostname or '').lower()
    if parsed.scheme!='https' or parsed.port not in (None,443) or parsed.username or not any(host==d or host.endswith('.'+d) for d in DOMAINS):
        raise ValueError('Unsupported avatar provider.')
    if any(not ipaddress.ip_address(row[4][0]).is_global for row in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)):
        raise ValueError('Avatar address is not public.')


class Redirect(HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,msg,headers,newurl):
        allowed(newurl)
        return super().redirect_request(request,fp,code,msg,headers,newurl)


def cache(work, identity, url, force=False):
    if not url: return ''
    key=hashlib.sha256(identity.encode()).hexdigest()+'.img'
    folder=work/'avatars';folder.mkdir(parents=True,exist_ok=True)
    path=folder/key
    if force or not path.exists() or path.stat().st_mtime<time.time()-86400:
        allowed(url)
        with build_opener(Redirect()).open(Request(url,headers={'User-Agent':'DriveCast'}),timeout=15) as response:
            kind=response.headers.get_content_type()
            if kind not in ('image/jpeg','image/png','image/webp','image/gif'): raise ValueError('Unsupported avatar format.')
            raw=response.read(2*1024*1024+1)
            if len(raw)>2*1024*1024: raise ValueError('Avatar is too large.')
        temporary=path.with_suffix('.tmp');temporary.write_bytes(raw);temporary.replace(path)
        path.with_suffix('.type').write_text(kind)
    return '/avatars/'+key
