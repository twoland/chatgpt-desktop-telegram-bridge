"""Optional existing-listener adapter. No bot token, polling or desktop access.

Copy beside local_mode.py with its reviewed hook. Disabled unless the private
board-governance.json exists. Uses a separate private Unix socket and HMAC key.
"""
import asyncio
import fcntl
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import socket
import time


class ServiceError(Exception):
    def __init__(self,message,*,retryable=True):
        super().__init__(message);self.retryable=retryable


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)


class UnixConnection(http.client.HTTPConnection):
    def __init__(self,path):super().__init__('localhost',timeout=5);self.path=str(path)
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(5);self.sock.connect(self.path)


class SignedClient:
    def __init__(self,socket_path,key_path,bridge_id):
        self.socket_path=Path(socket_path);self.key_path=Path(key_path);self.bridge_id=bridge_id
        if self.key_path.stat().st_mode&0o077:raise ValueError('Board transport key must be private.')
        self.key=bytes.fromhex(self.key_path.read_text().strip())
        if len(self.key)<32:raise ValueError('Board transport key is too short.')

    def _call(self,command):
        envelope={'bridge_id':self.bridge_id,'issued_at':time.time(),'nonce':secrets.token_urlsafe(24),'command':command}
        package={'envelope':envelope,'signature':hmac.new(self.key,canonical(envelope).encode(),hashlib.sha256).hexdigest()}
        connection=UnixConnection(self.socket_path)
        try:
            connection.request('POST','/_bridge',body=canonical(package).encode(),headers={'Content-Type':'application/json'})
            response=connection.getresponse();body=response.read(131073)
            if len(body)>131072:raise ServiceError('Board response too large.')
            value=json.loads(body)
            if response.status!=200:raise ServiceError(value.get('error','Board service rejected request.'),retryable=response.status not in (400,403,409))
            return value
        except (OSError,ValueError,http.client.HTTPException) as error:
            raise ServiceError('Board transport unavailable; inspect state before retrying.') from error
        finally:connection.close()

    async def call(self,command):return await asyncio.to_thread(self._call,command)


class GovernanceAdapter:
    def __init__(self,home,owner,client,send,clock=time.time):
        self.home=Path(home);self.home.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.home.stat().st_mode&0o077:raise ValueError('Governance adapter state directory must be private.')
        self.path=self.home/'journal.json';self.owner=owner;self.client=client;self.send=send;self.clock=clock
        self.lock_file=open(self.home/'adapter.lock','a+')
        os.chmod(str(self.home/'adapter.lock'),0o600)
        try:fcntl.flock(self.lock_file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close();raise ValueError('Another listener owns this governance journal. Stop it before restarting.')
        self.state=json.loads(self.path.read_text()) if self.path.exists() else {'owner':owner,'claim_id':None,'notifications':{},'messages':{}}
        if self.state['owner']!=owner:raise ValueError('Governance adapter belongs to another owner.')
        self.lock=asyncio.Lock()

    def close(self):
        self.lock_file.close()

    @classmethod
    def load(cls,config_path,*,owner,bridge_id,send):
        path=Path(config_path)
        if not path.exists():return None
        if path.is_symlink() or path.stat().st_mode&0o077:raise ValueError('Board governance configuration must be a private regular file.')
        data=json.loads(path.read_text())
        if data.get('enabled') is not True:return None
        if data.get('owner_user_id')!=owner or data.get('bridge_id')!=bridge_id:raise ValueError('Board governance owner/bridge does not match the paired listener.')
        for name in ('socket_path','transport_key_path','state_home'):
            if not Path(data[name]).is_absolute():raise ValueError('Board governance paths must be absolute.')
        client=SignedClient(data['socket_path'],data['transport_key_path'],bridge_id)
        return cls(data['state_home'],owner,client.call,send)

    def save(self):
        temp=self.path.with_suffix('.tmp')
        descriptor=os.open(str(temp),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(descriptor,'w') as handle:
            handle.write(canonical(self.state));handle.flush();os.fsync(handle.fileno())
        temp.replace(self.path)
        descriptor=os.open(str(self.home),os.O_RDONLY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)

    def accepts(self,message):
        sender=message.get('from') or {};chat=message.get('chat') or {}
        if sender.get('id')!=self.owner or chat.get('id')!=self.owner or chat.get('type')!='private' or sender.get('is_bot') or message.get('sender_chat'):return False
        text=str(message.get('text') or '').strip()
        explicit=bool(re.match(r'^/(?:board|board-auth)(?:@[A-Za-z0-9_]+)?(?:\s|$)',text,re.I))
        reply=(message.get('reply_to_message') or {}).get('message_id')
        words=text.casefold().rstrip('.!')
        bound=(words in ('yes','approve','approved','go ahead','no','decline','declined','deny','denied')
               and any(n.get('message_id')==reply for n in self.state['notifications'].values())) if type(reply) is int else False
        if not explicit and not bound:return False
        # Copied/forwarded commands never supply fresh human authority.
        if any(message.get(k) for k in ('forward_origin','forward_from','forward_from_chat','forward_sender_name','forward_date')):return False
        return True

    def accept_message(self,message):
        if not self.accepts(message):return False
        mid=message.get('message_id')
        if type(mid) is not int or mid<=0:return False
        sender=message['from'];chat=message['chat']
        data={'sender_id':sender['id'],'chat_id':chat['id'],'chat_type':chat['type'],'sender_is_bot':False,'forwarded':False,'message_id':mid,'date':message.get('date'),'text':message.get('text') or '', 'reply_to_message_id':(message.get('reply_to_message') or {}).get('message_id')}
        key=str(mid);old=self.state['messages'].get(key)
        fingerprint=hashlib.sha256(canonical(data).encode()).hexdigest()
        if old and old['fingerprint']!=fingerprint:raise ValueError('Telegram source identity changed; inspect instead of replaying.')
        if not old:
            self.state['messages'][key]={'message':data,'fingerprint':fingerprint,'status':'pending','attempts':0,'next_at':0}
        # A prior disk failure may have left only the in-memory entry. Persist
        # again before allowing the listener to advance Telegram's offset.
        self.save()
        return True

    def retry(self,entry,error):
        entry['attempts']=entry.get('attempts',0)+1
        entry['next_at']=self.clock()+max(min(2**min(entry['attempts'],8),300),float(getattr(error,'retry_after',None) or 0))
        entry['error']=str(error)[:300];self.save()

    async def deliver(self,entry,text):
        if entry.get('send_status') in ('sent','uncertain'):return
        if entry.get('send_status')=='sending':
            entry['send_status']='uncertain';entry['error']='Previous send has no receipt. Inspect Telegram before resending.';self.save();return
        entry['send_status']='sending';self.save()
        try:
            result=await self.send(chat_id=self.owner,text=text)
            mid=result.message_id if hasattr(result,'message_id') else result['message_id']
            if type(mid) is not int or mid<=0:raise ServiceError('Send has no confirmed message ID.')
        except asyncio.CancelledError:raise
        except Exception as error:
            # Retry only an API response that explicitly confirms non-delivery.
            if getattr(error,'retryable',False) and not getattr(error,'uncertain',True):
                entry['send_status']='pending';self.retry(entry,error)
            else:
                entry['send_status']='uncertain';entry['error']='Telegram send was not confirmed. No blind resend.';self.save()
            return
        entry['message_id']=mid;entry['send_status']='sent';entry['next_at']=0;self.save()

    async def tick(self):
        if self.lock.locked():return
        async with self.lock:
            # Replay service calls with a fresh transport nonce, preserving the
            # genuine Telegram source identity and idempotent decision key.
            for entry in list(self.state['messages'].values()):
                if entry.get('next_at',0)>self.clock():continue
                if entry['status']=='pending':
                    try:answer=await self.client({'action':'owner_message','message':entry['message']})
                    except ServiceError as error:
                        if error.retryable:self.retry(entry,error);continue
                        answer={'handled':True,'text':'Board request held: '+str(error),'decision_held':True}
                    secret='Agent credential (shown once):' in answer.get('text','')
                    entry['status']='decided';entry['answer']=dict(answer);entry['attempts']=0;entry['next_at']=0
                    if secret:entry['answer']['text']='Credential decision recorded. A one-time credential is not displayed again. If its delivery was not confirmed, revoke it and issue a new one.'
                    self.save()
                    if secret:
                        await self.deliver(entry,answer['text'])
                        # Do not retry a one-time secret, even after a definite
                        # failure. Its plaintext never enters the journal.
                        entry['status']='complete';entry.pop('answer',None);entry['message'].pop('text',None);self.save()
                        continue
                if entry['status']=='decided' and entry.get('answer',{}).get('text'):
                    await self.deliver(entry,entry['answer']['text'])
                    if entry.get('send_status') in ('sent','uncertain'):
                        entry['status']='complete'
                        # One-time credentials/pair codes are not retained in a
                        # durable bot acknowledgement after its send attempt.
                        entry.pop('answer',None);entry['message'].pop('text',None);self.save()
                elif entry['status']=='decided':entry['status']='complete';self.save()
            if self.state.get('next_claim_at',0)>self.clock():return
            if not self.state['claim_id']:
                self.state['claim_id']='claim_'+secrets.token_hex(16)
            self.save()  # A failed earlier write is not a durable claim identity.
            try:claimed=await self.client({'action':'claim_notifications','claim_id':self.state['claim_id']})
            except ServiceError as error:
                self.state['claim_attempts']=self.state.get('claim_attempts',0)+1
                self.state['next_claim_at']=self.clock()+min(2**min(self.state['claim_attempts'],8),300)
                self.save();return
            for n in claimed.get('notifications',[]):
                if n.get('chat_id')!=self.owner:raise ValueError('Board tried to send a notification outside its paired owner DM.')
                self.state['notifications'].setdefault(str(n['notification_id']),dict(n,send_status='pending',attempts=0,next_at=0))
            self.state['claim_id']=None;self.state['claim_attempts']=0;self.state['next_claim_at']=0;self.save()
            for entry in list(self.state['notifications'].values()):
                if entry.get('acknowledged') or entry.get('next_at',0)>self.clock():continue
                await self.deliver(entry,entry['text'])
                status=entry.get('send_status')
                if status not in ('sent','uncertain'):continue
                command={'action':'notification_result','notification_id':entry['notification_id'],'status':status}
                if status=='sent':command['message_id']=entry['message_id']
                try:await self.client(command)
                except ServiceError as error:self.retry(entry,error);continue
                entry['acknowledged']=True;entry.pop('text',None);self.save()
