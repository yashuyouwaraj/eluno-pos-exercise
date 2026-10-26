"""Durable, conservative refunds and cash-till operations."""
from contextlib import contextmanager
from datetime import date, datetime, timezone
import json, sqlite3, threading
import hashlib
from typing import Any, Dict, List, Optional, Protocol, Tuple
from uuid import uuid4
from .adapters.http import HttpRequestError
from .business_time import bengaluru_business_date
from .money import paise_from_decimal, decimal_string_from_paise

class OperationsError(ValueError): pass
CASH_VARIANCE_THRESHOLD_PAISE = 20000
class ShopifySource(Protocol):
    def get_order(self, order_id: int) -> Dict[str, Any]: ...
    def create_refund(self, order_id: int, refund: Dict[str, Any]) -> Dict[str, Any]: ...
    def list_locations(self) -> List[Dict[str, Any]]: ...

def now(): return datetime.now(timezone.utc).isoformat()

class OperationsStore:
    def __init__(self, path):
        self.path, self.lock = str(path), threading.RLock()
        with self.session() as c:
            c.execute("PRAGMA journal_mode=WAL")
            schema=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='refunds'").fetchone()
            if schema and 'order_id TEXT UNIQUE' in schema[0]:
                c.execute("ALTER TABLE refunds RENAME TO refunds_legacy")
            c.execute("CREATE TABLE IF NOT EXISTS refunds(refund_id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,order_id TEXT NOT NULL,status TEXT NOT NULL,store_code TEXT NOT NULL,operator TEXT NOT NULL,reason TEXT NOT NULL,credit_note TEXT NOT NULL,amount_paise INTEGER NOT NULL,tax_paise INTEGER NOT NULL,lines_json TEXT NOT NULL DEFAULT '[]',shopify_refund_id TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)")
            if schema and 'order_id TEXT UNIQUE' in schema[0]:
                c.execute("INSERT INTO refunds(refund_id,request_key,order_id,status,store_code,operator,reason,credit_note,amount_paise,tax_paise,shopify_refund_id,error,created_at,updated_at) SELECT refund_id,'legacy:'||refund_id,order_id,status,store_code,operator,reason,credit_note,amount_paise,tax_paise,shopify_refund_id,error,created_at,updated_at FROM refunds_legacy")
                c.execute("DROP TABLE refunds_legacy")
            for column in ("cash_till_id TEXT", "cash_post_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED'", "request_hash TEXT"):
                try: c.execute("ALTER TABLE refunds ADD COLUMN " + column)
                except sqlite3.OperationalError: pass
            c.execute("CREATE TABLE IF NOT EXISTS credit_series(store_code TEXT PRIMARY KEY,next_number INTEGER NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS till_sessions(session_id TEXT PRIMARY KEY,store_code TEXT NOT NULL,location_id TEXT NOT NULL,operator TEXT NOT NULL,status TEXT NOT NULL,opening_paise INTEGER NOT NULL,counted_paise INTEGER,variance_paise INTEGER,created_at TEXT NOT NULL,closed_at TEXT)")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_open_till ON till_sessions(store_code) WHERE status='OPEN'")
            c.execute("CREATE TABLE IF NOT EXISTS cash_movements(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,type TEXT NOT NULL,amount_paise INTEGER NOT NULL,reason TEXT,operator TEXT NOT NULL,ref TEXT UNIQUE,created_at TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS audit_events(event_id TEXT PRIMARY KEY,occurred_at TEXT NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL,entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,reason TEXT,metadata_json TEXT NOT NULL)")
    @contextmanager
    def session(self):
        c=sqlite3.connect(self.path,timeout=10,isolation_level=None); c.row_factory=sqlite3.Row
        try: yield c
        finally: c.close()
    def refund_create(self, order_id, store, operator, reason, amount, tax, request_key=None, lines=None, request_hash=None):
        request_key=request_key or 'full:'+str(order_id); lines=lines or []
        with self.lock, self.session() as c:
            c.execute("BEGIN IMMEDIATE"); row=c.execute("SELECT * FROM refunds WHERE request_key=?",(request_key,)).fetchone()
            if row:
                if not request_hash or row["request_hash"] != request_hash:
                    c.execute("ROLLBACK")
                    raise OperationsError('idempotency key was already used for a different or unknown refund request')
                c.execute("COMMIT"); return dict(row),False
            seq=c.execute("SELECT next_number FROM credit_series WHERE store_code=?",(store,)).fetchone(); n=int(seq[0]) if seq else 1
            c.execute("INSERT INTO credit_series(store_code,next_number) VALUES (?,?) ON CONFLICT(store_code) DO UPDATE SET next_number=excluded.next_number",(store,n+1))
            rid='refund_'+uuid4().hex; t=now(); credit='ELU/{}/FY26/CN/{:05d}'.format(store,n)
            c.execute("INSERT INTO refunds(refund_id,request_key,order_id,status,store_code,operator,reason,credit_note,amount_paise,tax_paise,lines_json,created_at,updated_at,request_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(rid,request_key,str(order_id),'PENDING',store,operator,reason,credit,amount,tax,json.dumps(lines,separators=(',',':')),t,t,request_hash))
            self._audit_in_session(c,operator,'REFUND_SUBMITTED','refund',rid,reason=reason,metadata={'order_id':str(order_id),'amount_paise':amount})
            c.execute("COMMIT")
            return self.refund_get(rid),True
    def refund_get(self,rid):
        with self.session() as c: r=c.execute("SELECT * FROM refunds WHERE refund_id=?",(rid,)).fetchone()
        if not r: raise OperationsError('refund was not found')
        return dict(r)
    def refund_update(self,rid,status,audit_action=None,audit_actor=None,audit_reason=None,audit_metadata=None,**vals):
        with self.lock,self.session() as c:
            c.execute("BEGIN IMMEDIATE"); cols=['status=?','updated_at=?']; args=[status,now()]
            for k,v in vals.items(): cols.append(k+'=?');args.append(v)
            args.append(rid);c.execute('UPDATE refunds SET '+','.join(cols)+' WHERE refund_id=?',args)
            if audit_action:
                self._audit_in_session(c,audit_actor,audit_action,'refund',rid,audit_reason,audit_metadata)
            c.execute('COMMIT')
        return self.refund_get(rid)
    def audit(self, actor, action, entity_type, entity_id, reason=None, metadata=None):
        if not actor or not action or not entity_type or not entity_id: raise OperationsError('audit event requires actor, action, and entity reference')
        with self.lock,self.session() as c:
            self._audit_in_session(c, actor, action, entity_type, entity_id, reason, metadata)
    @staticmethod
    def _audit_in_session(c, actor, action, entity_type, entity_id, reason=None, metadata=None):
        if not actor or not action or not entity_type or not entity_id: raise OperationsError('audit event requires actor, action, and entity reference')
        c.execute('INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?)',(uuid4().hex,now(),str(actor),str(action),str(entity_type),str(entity_id),reason,json.dumps(metadata or {},separators=(',',':'))))
    def audit_events(self, entity_id=None):
        with self.session() as c:
            rows=c.execute('SELECT * FROM audit_events'+(' WHERE entity_id=?' if entity_id else '')+' ORDER BY occurred_at,event_id',(() if entity_id is None else (str(entity_id),))).fetchall()
        return [dict(r) for r in rows]
    def open_till(self,store,location,operator,float_paise):
        if float_paise<0: raise OperationsError('opening float cannot be negative')
        sid='till_'+uuid4().hex; t=now()
        try:
            with self.lock,self.session() as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute("INSERT INTO till_sessions VALUES (?,?,?,?, 'OPEN', ?,NULL,NULL,?,NULL)",(sid,store,str(location),operator,float_paise,t))
                self._audit_in_session(c,operator,'TILL_OPENED','till',sid,metadata={'store_code':store,'location_id':str(location),'opening_float_paise':float_paise})
                c.execute('COMMIT')
        except sqlite3.IntegrityError as e: raise OperationsError('a till session is already open for this store') from e
        return self.till_get(sid)
    def till_get(self,sid):
        with self.session() as c: s=c.execute('SELECT * FROM till_sessions WHERE session_id=?',(sid,)).fetchone()
        if not s: raise OperationsError('till session was not found')
        d=dict(s); d['expected_paise']=d['opening_paise']+self._sum(sid); return d
    def open_tills(self,store_code):
        with self.session() as c:
            rows=c.execute(
                "SELECT session_id FROM till_sessions WHERE store_code=? AND status='OPEN' ORDER BY created_at",
                (store_code,),
            ).fetchall()
        return [self.till_get(row['session_id']) for row in rows]
    def _sum(self,sid):
        with self.session() as c:return int(c.execute('SELECT COALESCE(SUM(amount_paise),0) FROM cash_movements WHERE session_id=?',(sid,)).fetchone()[0])
    def movement_exists(self, ref):
        with self.session() as c:return c.execute('SELECT 1 FROM cash_movements WHERE ref=?',(ref,)).fetchone() is not None
    def tills_for(self, location_id, business_date):
        with self.session() as c: rows=c.execute("SELECT * FROM till_sessions WHERE location_id=?",(str(location_id),)).fetchall()
        rows = [
            row for row in rows
            if bengaluru_business_date(row["created_at"]) == date.fromisoformat(business_date)
            or (
                row["closed_at"]
                and bengaluru_business_date(row["closed_at"]) == date.fromisoformat(business_date)
            )
        ]
        return [self.till_get(r['session_id']) for r in rows]
    def cash_variance_alert(self, till):
        variance = till.get("variance_paise")
        closed_at = till.get("closed_at")
        if (
            till.get("status") != "CLOSED"
            or variance is None
            or abs(int(variance)) <= CASH_VARIANCE_THRESHOLD_PAISE
            or not closed_at
        ):
            return None
        return {
            "id": "cash-variance:" + till["session_id"],
            "type": "cash_variance_threshold_exceeded",
            "severity": "high",
            "store_code": till["store_code"],
            "session_id": till["session_id"],
            "expected_cash_paise": int(till["expected_paise"]),
            "counted_cash_paise": int(till["counted_paise"]),
            "variance_paise": int(variance),
            "threshold_paise": CASH_VARIANCE_THRESHOLD_PAISE,
            "business_date": bengaluru_business_date(closed_at).isoformat(),
            "closed_at": closed_at,
            "explanation": "Closed till cash variance exceeds Rs. 200.00; Finance review is required.",
        }
    def cash_variance_alerts(self, location_id, business_date):
        target = date.fromisoformat(business_date) if isinstance(business_date, str) else business_date
        with self.session() as c:
            rows = c.execute(
                "SELECT * FROM till_sessions WHERE location_id=? AND status='CLOSED'",
                (str(location_id),),
            ).fetchall()
        alerts = []
        for row in rows:
            till = self.till_get(row["session_id"])
            alert = self.cash_variance_alert(till)
            if alert and alert["business_date"] == target.isoformat():
                alerts.append(alert)
        return sorted(alerts, key=lambda alert: alert["session_id"])
    def refunds_for(self, store_code):
        with self.session() as c:return [dict(r) for r in c.execute('SELECT * FROM refunds WHERE store_code=?',(store_code,)).fetchall()]
    def movement(self,sid,kind,amount,operator,reason=None,ref=None):
        if kind not in ('paid_in','paid_out','cash_sale','cash_refund') or amount<=0: raise OperationsError('invalid cash movement')
        signed=-amount if kind in ('paid_out','cash_refund') else amount
        try:
            with self.lock,self.session() as c:
                c.execute('BEGIN IMMEDIATE'); s=c.execute('SELECT status FROM till_sessions WHERE session_id=?',(sid,)).fetchone()
                if not s or s['status']!='OPEN': c.execute('ROLLBACK'); raise OperationsError('till session is not open')
                c.execute('INSERT INTO cash_movements VALUES (?,?,?,?,?,?,?,?)',(uuid4().hex,sid,kind,signed,reason,operator,ref,now()))
                self._audit_in_session(c,operator,'TILL_MOVEMENT','till',sid,reason=reason,metadata={'type':kind,'amount_paise':signed,'ref':ref})
                c.execute('COMMIT')
        except sqlite3.IntegrityError as e: raise OperationsError('cash movement reference already recorded') from e
        return self.till_get(sid)
    def close_till(self,sid,operator,counted):
        if counted<0: raise OperationsError('counted cash cannot be negative')
        with self.lock,self.session() as c:
            c.execute('BEGIN IMMEDIATE'); s=c.execute('SELECT * FROM till_sessions WHERE session_id=?',(sid,)).fetchone()
            if not s or s['status']!='OPEN': c.execute('ROLLBACK'); raise OperationsError('till session is not open')
            total=int(c.execute('SELECT COALESCE(SUM(amount_paise),0) FROM cash_movements WHERE session_id=?',(sid,)).fetchone()[0]); expected=s['opening_paise']+total
            variance=counted-expected
            c.execute("UPDATE till_sessions SET status='CLOSED',counted_paise=?,variance_paise=?,closed_at=? WHERE session_id=?",(counted,variance,now(),sid))
            self._audit_in_session(c,operator,'TILL_CLOSED','till',sid,metadata={'counted_cash_paise':counted,'variance_paise':variance})
            c.execute('COMMIT')
        return self.till_get(sid)

class RefundService:
    def __init__(self, shopify:ShopifySource, store:OperationsStore, tills:Optional[OperationsStore]=None): self.shopify,self.store,self.tills=shopify,store,tills or store
    def refund(self,order_id,store_code,operator,reason,till_session_id=None, items=None, idempotency_key=None):
        if not reason: raise OperationsError('reason is required')
        order=self.shopify.get_order(int(order_id)); self._belongs(order,store_code)
        sales=[t for t in order.get('transactions',[]) if t.get('kind')=='sale' and t.get('status')=='success']
        amount=sum(paise_from_decimal(t['amount']) for t in sales); total=paise_from_decimal(order['total_price'])
        if amount<=0 or amount!=total: raise OperationsError('order is not fully paid and eligible for a full refund')
        full_request=items is None
        requested={str(x['line_item_id']):int(x['quantity']) for x in (items or [{'line_item_id':x['id'],'quantity':x['quantity']} for x in order['line_items']])}
        if not requested or any(q<=0 for q in requested.values()): raise OperationsError('refund item quantities must be positive')
        by_id={str(x['id']):x for x in order['line_items']}; prior={}
        for refund in order.get('refunds',[]):
            for line in refund.get('refund_line_items',[]): prior[str(line['line_item_id'])]=prior.get(str(line['line_item_id']),0)+int(line['quantity'])
        lines=[]; amount=tax=0
        for line_id,qty in requested.items():
            li=by_id.get(line_id)
            if not li: raise OperationsError('line item is not on this order')
            if qty>int(li['quantity'])-prior.get(line_id,0): raise OperationsError('refund quantity exceeds remaining eligible quantity')
            gross=(paise_from_decimal(li['price'])*int(li['quantity'])-paise_from_decimal(li.get('total_discount','0.00')))*qty//int(li['quantity']) if 'price' in li else 0
            line_tax=sum(paise_from_decimal(x['price']) for x in li.get('tax_lines',[]))*qty//int(li['quantity'])
            props={p.get('name'):p.get('value') for p in li.get('properties',[])}; frame=props.get('line_type')=='frame'
            lines.append({'line_item_id':li['id'],'quantity':qty,'restock_type':'return' if frame else 'no_restock',**({'location_id':order['location_id']} if frame else {})})
            amount+=gross; tax+=line_tax
        if full_request and not all('price' in x for x in order['line_items']):
            amount=total
        request_key=idempotency_key or ('full:'+str(order_id) if full_request else 'partial:'+str(order_id)+':'+json.dumps(sorted(requested.items())))
        request_data=json.dumps({'order_id':str(order_id),'store_code':store_code,'reason':reason,'full_request':full_request,'till_session_id':till_session_id,'amount_paise':amount,'tax_paise':tax,'lines':sorted(lines,key=lambda line:str(line['line_item_id']))},sort_keys=True,separators=(',',':'))
        request_hash=hashlib.sha256(request_data.encode('utf-8')).hexdigest()
        row,created=self.store.refund_create(order_id,store_code,operator,reason,amount,tax,request_key,lines,request_hash)
        if not created: return row
        tx=sales[0]
        payload={'refund_line_items':lines,'transactions':[{'kind':'refund','amount':decimal_string_from_paise(amount),'gateway':tx.get('gateway'),'parent_id':tx.get('id')}],'note':('Full refund: ' if full_request else 'Partial refund: ')+reason,'note_attributes':[{'name':'credit_note_no','value':row['credit_note']},{'name':'reason_code','value':reason}]}
        try: remote=self.shopify.create_refund(int(order_id),payload)
        except HttpRequestError:
            return self.store.refund_update(row['refund_id'],'REFUND_UNKNOWN',error='Shopify refund outcome unknown',audit_action='REFUND_OUTCOME_UNKNOWN',audit_actor=operator,audit_reason=reason)
        row=self.store.refund_update(row['refund_id'],'COMPLETED',shopify_refund_id=str(remote.get('id')), cash_till_id=till_session_id, cash_post_status=('PENDING' if till_session_id and tx.get('gateway')=='cash' else 'NOT_REQUIRED'))
        if row['cash_post_status']=='PENDING': row=self._post_cash_refund(row)
        return self.store.refund_update(row['refund_id'],row['status'],audit_action='REFUND_COMPLETED',audit_actor=operator,audit_reason=reason)
    def status(self,rid): return self.store.refund_get(rid)
    def recover(self,rid):
        r=self.store.refund_get(rid)
        if r['status']=='PENDING':
            return self.store.refund_update(rid,'REFUND_UNKNOWN',error='restart during Shopify refund request',audit_action='REFUND_RECOVERED_UNKNOWN',audit_actor=r['operator'],audit_reason=r['reason'])
        if r['status']=='COMPLETED' and r['cash_post_status']=='PENDING': return self._post_cash_refund(r)
        if r['status']!='REFUND_UNKNOWN': raise OperationsError('refund is not awaiting reconciliation')
        return r
    def _post_cash_refund(self,row):
        if self.tills.movement_exists(row['refund_id']):
            return self.store.refund_update(row['refund_id'],'COMPLETED',cash_post_status='POSTED',error=None)
        try: self.tills.movement(row['cash_till_id'],'cash_refund',int(row['amount_paise']),row['operator'],row['reason'],row['refund_id'])
        except OperationsError as exc: return self.store.refund_update(row['refund_id'],'COMPLETED',cash_post_status='PENDING',error='cash refund posting pending: '+str(exc))
        return self.store.refund_update(row['refund_id'],'COMPLETED',cash_post_status='POSTED',error=None)
    def _belongs(self,order,store):
        attrs={a.get('name'):a.get('value') for a in order.get('note_attributes',[])}
        if attrs.get('store_code')!=store: raise OperationsError('order does not belong to this store')
        locations=[x for x in self.shopify.list_locations() if str(x.get('id'))==str(order.get('location_id'))]
        if len(locations)!=1 or (locations[0].get('eluno') or {}).get('store_code')!=store:
            raise OperationsError('order location does not belong to this store')
