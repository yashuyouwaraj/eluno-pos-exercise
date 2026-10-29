"""Durable, conservative refunds and cash-till operations."""
from contextlib import contextmanager
from datetime import date, datetime, timezone
import json, sqlite3, threading
import hashlib
from pathlib import Path
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

class TerminalRefundSource(Protocol):
    def refund(self, charge_id: str, amount_paise: int, reason: str, idempotency_key: str) -> Dict[str, Any]: ...

def now(): return datetime.now(timezone.utc).isoformat()

class OperationsStore:
    def __init__(self, path):
        self.path, self.lock = str(path), threading.RLock()
        with self.session() as c:
            c.execute("PRAGMA journal_mode=WAL")
            schema=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='refunds'").fetchone()
            if schema and 'order_id TEXT UNIQUE' in schema[0]:
                c.execute("ALTER TABLE refunds RENAME TO refunds_legacy")
            c.execute("CREATE TABLE IF NOT EXISTS refunds(refund_id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,order_id TEXT NOT NULL,status TEXT NOT NULL,store_code TEXT NOT NULL,operator TEXT NOT NULL,reason TEXT NOT NULL,credit_note TEXT NOT NULL,amount_paise INTEGER NOT NULL,tax_paise INTEGER NOT NULL,lines_json TEXT NOT NULL DEFAULT '[]',shopify_refund_id TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,invoice_no TEXT)")
            if schema and 'order_id TEXT UNIQUE' in schema[0]:
                c.execute("INSERT INTO refunds(refund_id,request_key,order_id,status,store_code,operator,reason,credit_note,amount_paise,tax_paise,shopify_refund_id,error,created_at,updated_at) SELECT refund_id,'legacy:'||refund_id,order_id,status,store_code,operator,reason,credit_note,amount_paise,tax_paise,shopify_refund_id,error,created_at,updated_at FROM refunds_legacy")
                c.execute("DROP TABLE refunds_legacy")
            for column in ("cash_till_id TEXT", "cash_post_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED'", "request_hash TEXT", "invoice_no TEXT", "terminal_refund_id TEXT"):
                try: c.execute("ALTER TABLE refunds ADD COLUMN " + column)
                except sqlite3.OperationalError: pass
            c.execute("CREATE TABLE IF NOT EXISTS credit_series(store_code TEXT PRIMARY KEY,next_number INTEGER NOT NULL)")
            c.execute("""CREATE TABLE IF NOT EXISTS till_sessions(
                session_id TEXT PRIMARY KEY,
                store_code TEXT NOT NULL,
                location_id TEXT NOT NULL,
                operator TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('OPEN','CLOSED')),
                opening_paise INTEGER NOT NULL CHECK(opening_paise >= 0),
                counted_paise INTEGER,
                variance_paise INTEGER,
                created_at TEXT NOT NULL,
                closed_at TEXT,
                closed_by TEXT,
                review_note TEXT,
                review_status TEXT NOT NULL DEFAULT 'UNREVIEWED',
                is_fixture INTEGER NOT NULL DEFAULT 0 CHECK(is_fixture IN (0,1))
            )""")
            till_columns = {
                row["name"] for row in c.execute("PRAGMA table_info(till_sessions)")
            }
            for name, declaration in (
                ("closed_by", "TEXT"),
                ("review_note", "TEXT"),
                ("review_status", "TEXT NOT NULL DEFAULT 'UNREVIEWED'"),
                ("is_fixture", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in till_columns:
                    c.execute("ALTER TABLE till_sessions ADD COLUMN {} {}".format(name, declaration))
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_open_till ON till_sessions(store_code) WHERE status='OPEN'")
            c.execute("""CREATE TABLE IF NOT EXISTS cash_movements(
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES till_sessions(session_id),
                type TEXT NOT NULL CHECK(type IN ('paid_in','paid_out','cash_sale','cash_refund')),
                amount_paise INTEGER NOT NULL CHECK(amount_paise != 0),
                reason TEXT,
                operator TEXT NOT NULL,
                ref TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )""")
            c.execute("CREATE TABLE IF NOT EXISTS audit_events(event_id TEXT PRIMARY KEY,occurred_at TEXT NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL,entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,reason TEXT,metadata_json TEXT NOT NULL)")
    @contextmanager
    def session(self):
        c=sqlite3.connect(self.path,timeout=10,isolation_level=None); c.row_factory=sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try: yield c
        finally: c.close()
    def refund_create(self, order_id, store, operator, reason, amount, tax, request_key=None, lines=None, request_hash=None, invoice_no=None):
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
            c.execute("INSERT INTO refunds(refund_id,request_key,order_id,status,store_code,operator,reason,credit_note,amount_paise,tax_paise,lines_json,created_at,updated_at,request_hash,invoice_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(rid,request_key,str(order_id),'PENDING',store,operator,reason,credit,amount,tax,json.dumps(lines,separators=(',',':')),t,t,request_hash,invoice_no))
            self._audit_in_session(c,operator,'REFUND_SUBMITTED','refund',rid,reason=reason,metadata={'order_id':str(order_id),'amount_paise':amount})
            c.execute("COMMIT")
            return self.refund_get(rid),True
    def refund_by_request_key(self, request_key):
        with self.session() as c:
            row=c.execute("SELECT * FROM refunds WHERE request_key=?",(request_key,)).fetchone()
        return None if row is None else dict(row)
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
        if isinstance(float_paise,bool) or not isinstance(float_paise,int) or float_paise<0:
            raise OperationsError('opening float must be a nonnegative integer paise amount')
        if not isinstance(store,str) or not store.strip() or not str(location).strip():
            raise OperationsError('store and location are required')
        if not isinstance(operator,str) or not operator.strip():
            raise OperationsError('opening operator is required')
        sid='till_'+uuid4().hex; t=now()
        try:
            with self.lock,self.session() as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute(
                    """INSERT INTO till_sessions
                       (session_id,store_code,location_id,operator,status,opening_paise,created_at)
                       VALUES (?,?,?,?,'OPEN',?,?)""",
                    (sid,store.strip(),str(location),operator.strip(),float_paise,t),
                )
                self._audit_in_session(c,operator,'TILL_OPENED','till',sid,metadata={'store_code':store,'location_id':str(location),'opening_float_paise':float_paise})
                c.execute('COMMIT')
        except sqlite3.IntegrityError as e: raise OperationsError('a till session is already open for this store') from e
        return self.till_get(sid)
    def till_get(self,sid):
        with self.session() as c: s=c.execute('SELECT * FROM till_sessions WHERE session_id=?',(sid,)).fetchone()
        if not s: raise OperationsError('till session was not found')
        d=dict(s)
        d['opened_at']=d['created_at']
        d['opened_by']=d['operator']
        d['opening_float_paise']=int(d['opening_paise'])
        d['counted_cash_paise']=d['counted_paise']
        d['expected_paise']=d['opening_paise']+self._sum(sid)
        with self.session() as c:
            d['movements']=[
                dict(row) for row in c.execute(
                    'SELECT * FROM cash_movements WHERE session_id=? ORDER BY created_at,id',
                    (sid,),
                ).fetchall()
            ]
        return d
    def open_tills(self,store_code):
        with self.session() as c:
            rows=c.execute(
                "SELECT session_id FROM till_sessions WHERE store_code=? AND status='OPEN' ORDER BY created_at",
                (store_code,),
            ).fetchall()
        return [self.till_get(row['session_id']) for row in rows]
    def list_till_sessions(self,store_code=None,include_fixtures=True):
        clauses=[]; args=[]
        if store_code:
            clauses.append("store_code=?"); args.append(store_code)
        if not include_fixtures:
            clauses.append("is_fixture=0")
        query='SELECT session_id FROM till_sessions'
        if clauses:
            query+=' WHERE '+' AND '.join(clauses)
        query+=' ORDER BY created_at DESC,session_id'
        with self.session() as c:
            rows=c.execute(query,args).fetchall()
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
    def refunds_for_order(self, order_id):
        with self.session() as c:
            return [dict(row) for row in c.execute(
                'SELECT * FROM refunds WHERE order_id=? ORDER BY created_at,refund_id',
                (str(order_id),),
            ).fetchall()]
    def movement(self,sid,kind,amount,operator,reason=None,ref=None):
        if (
            kind not in ('paid_in','paid_out','cash_sale','cash_refund')
            or isinstance(amount,bool) or not isinstance(amount,int) or amount<=0
        ):
            raise OperationsError('cash movement requires a supported type and positive integer paise amount')
        if not isinstance(operator,str) or not operator.strip():
            raise OperationsError('cash movement operator is required')
        if not isinstance(ref,str) or not ref.strip() or len(ref.strip())>160:
            raise OperationsError('cash movement requires a unique reference')
        if kind in ('paid_in','paid_out') and (
            not isinstance(reason,str) or not reason.strip() or len(reason.strip())>400
        ):
            raise OperationsError('paid-in and paid-out movements require a reason')
        signed=-amount if kind in ('paid_out','cash_refund') else amount
        try:
            with self.lock,self.session() as c:
                c.execute('BEGIN IMMEDIATE'); s=c.execute('SELECT status FROM till_sessions WHERE session_id=?',(sid,)).fetchone()
                if not s or s['status']!='OPEN': c.execute('ROLLBACK'); raise OperationsError('till session is not open')
                c.execute('INSERT INTO cash_movements VALUES (?,?,?,?,?,?,?,?)',(uuid4().hex,sid,kind,signed,reason,operator.strip(),ref.strip(),now()))
                self._audit_in_session(c,operator.strip(),'TILL_MOVEMENT','till',sid,reason=reason,metadata={'type':kind,'amount_paise':signed,'ref':ref.strip()})
                c.execute('COMMIT')
        except sqlite3.IntegrityError as e: raise OperationsError('cash movement reference already recorded') from e
        return self.till_get(sid)
    def close_till(self,sid,operator,counted,review_note=None):
        if isinstance(counted,bool) or not isinstance(counted,int) or counted<0:
            raise OperationsError('counted cash must be a nonnegative integer paise amount')
        if not isinstance(operator,str) or not operator.strip():
            raise OperationsError('closing operator is required')
        if review_note is not None and (
            not isinstance(review_note,str) or len(review_note.strip())>400
        ):
            raise OperationsError('review note must be at most 400 characters')
        with self.lock,self.session() as c:
            c.execute('BEGIN IMMEDIATE'); s=c.execute('SELECT * FROM till_sessions WHERE session_id=?',(sid,)).fetchone()
            if not s or s['status']!='OPEN': c.execute('ROLLBACK'); raise OperationsError('till session is not open')
            total=int(c.execute('SELECT COALESCE(SUM(amount_paise),0) FROM cash_movements WHERE session_id=?',(sid,)).fetchone()[0]); expected=s['opening_paise']+total
            variance=counted-expected
            review_status='CLEAR' if variance==0 else 'REVIEW_REQUIRED'
            note=review_note.strip() if isinstance(review_note,str) and review_note.strip() else (
                'Cash variance requires manager review.' if variance else None
            )
            closed_at=now()
            c.execute(
                """UPDATE till_sessions SET status='CLOSED',counted_paise=?,variance_paise=?,
                   closed_at=?,closed_by=?,review_note=?,review_status=? WHERE session_id=?""",
                (counted,variance,closed_at,operator.strip(),note,review_status,sid),
            )
            self._audit_in_session(c,operator.strip(),'TILL_CLOSED','till',sid,reason=note,metadata={'counted_cash_paise':counted,'expected_cash_paise':expected,'variance_paise':variance,'review_status':review_status})
            c.execute('COMMIT')
        return self.till_get(sid)
    def review_till(self,sid,operator,note):
        if not isinstance(note,str) or not note.strip() or len(note.strip())>400:
            raise OperationsError('a review note between 1 and 400 characters is required')
        with self.lock,self.session() as c:
            c.execute('BEGIN IMMEDIATE')
            session=c.execute("SELECT status,is_fixture FROM till_sessions WHERE session_id=?",(sid,)).fetchone()
            if not session or session['status']!='CLOSED':
                c.execute('ROLLBACK'); raise OperationsError('only a closed till can be reviewed')
            c.execute(
                "UPDATE till_sessions SET review_status='REVIEWED',review_note=? WHERE session_id=?",
                (note.strip(),sid),
            )
            self._audit_in_session(
                c,operator,'TILL_REVIEWED','till',sid,reason=note.strip(),
                metadata={'fixture':bool(session['is_fixture'])},
            )
            c.execute('COMMIT')
        return self.till_get(sid)
    def load_till_fixture(self,path):
        try:
            fixture=json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError,ValueError) as exc:
            raise OperationsError('till fixture could not be read as JSON') from exc
        if not isinstance(fixture,list):
            raise OperationsError('till fixture must contain a list of sessions')
        imported=0; skipped=0
        with self.lock,self.session() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                for item in fixture:
                    if not isinstance(item,dict):
                        raise OperationsError('till fixture contains an invalid session')
                    sid=item.get('session_id')
                    if not isinstance(sid,str) or not sid.strip():
                        raise OperationsError('fixture session ID is required')
                    if c.execute('SELECT 1 FROM till_sessions WHERE session_id=?',(sid,)).fetchone():
                        skipped+=1
                        continue
                    store=item.get('store_code')
                    location=item.get('location_id')
                    opened_at=item.get('opened_at')
                    opened_by=item.get('opened_by')
                    opening=item.get('opening_float_paise')
                    movements=item.get('movements')
                    closed_at=item.get('closed_at')
                    closed_by=item.get('closed_by')
                    counted=item.get('counted_cash_paise')
                    expected=item.get('expected_cash_paise')
                    variance=item.get('variance_paise')
                    if (
                        not isinstance(store,str) or not store
                        or isinstance(location,bool) or not isinstance(location,(str,int))
                        or not isinstance(opened_by,str) or not opened_by
                        or not isinstance(movements,list)
                        or isinstance(opening,bool) or not isinstance(opening,int) or opening<0
                    ):
                        raise OperationsError('fixture session has invalid store, operator, opening float, or movements')
                    try:
                        datetime.fromisoformat(opened_at)
                        if closed_at is not None:
                            datetime.fromisoformat(closed_at)
                    except (TypeError,ValueError) as exc:
                        raise OperationsError('fixture timestamps must preserve valid ISO-8601 values') from exc
                    if closed_at is not None and (
                        not isinstance(closed_by,str) or not closed_by
                        or any(isinstance(v,bool) or not isinstance(v,int) for v in (counted,expected,variance))
                    ):
                        raise OperationsError('closed fixture sessions require counted, expected, and variance paise values')
                    movement_total=0
                    for movement in movements:
                        if not isinstance(movement,dict):
                            raise OperationsError('fixture contains an invalid cash movement')
                        kind=movement.get('type')
                        amount=movement.get('amount_paise')
                        ref=movement.get('ref')
                        at=movement.get('at')
                        reason=movement.get('reason')
                        if (
                            kind not in ('cash_sale','cash_refund','paid_in','paid_out')
                            or isinstance(amount,bool) or not isinstance(amount,int) or amount==0
                            or not isinstance(ref,str) or not ref
                            or not isinstance(at,str)
                        ):
                            raise OperationsError('fixture movement has invalid type, amount, reference, or timestamp')
                        try:
                            datetime.fromisoformat(at)
                        except ValueError as exc:
                            raise OperationsError('fixture movement timestamp must be valid ISO-8601') from exc
                        if (kind in ('cash_sale','paid_in') and amount<0) or (
                            kind in ('cash_refund','paid_out') and amount>0
                        ):
                            raise OperationsError('fixture movements must use signed paise amounts consistently')
                        if kind in ('paid_in','paid_out') and (not isinstance(reason,str) or not reason.strip()):
                            raise OperationsError('fixture paid-in and paid-out movements require a reason')
                        movement_total+=amount
                    calculated=opening+movement_total
                    if closed_at is not None and (
                        expected!=calculated or variance!=counted-expected
                    ):
                        raise OperationsError('fixture declared totals do not reconcile to its persisted movements')
                    status='CLOSED' if closed_at is not None else 'OPEN'
                    review_status=('CLEAR' if variance==0 else 'REVIEW_REQUIRED') if status=='CLOSED' else 'UNREVIEWED'
                    c.execute(
                        """INSERT INTO till_sessions
                           (session_id,store_code,location_id,operator,status,opening_paise,counted_paise,
                            variance_paise,created_at,closed_at,closed_by,review_note,review_status,is_fixture)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                        (sid,store,str(location),opened_by,status,opening,counted,variance,opened_at,
                         closed_at,closed_by,item.get('variance_note'),review_status),
                    )
                    for movement in movements:
                        c.execute(
                            """INSERT INTO cash_movements
                               (id,session_id,type,amount_paise,reason,operator,ref,created_at)
                               VALUES (?,?,?,?,?,?,?,?)""",
                            (uuid4().hex,sid,movement['type'],movement['amount_paise'],
                             movement.get('reason'),opened_by,movement['ref'],movement['at']),
                        )
                    imported+=1
                c.execute('COMMIT')
            except Exception:
                c.execute('ROLLBACK')
                raise
        return {'imported':imported,'skipped_existing':skipped}

class RefundService:
    def __init__(
        self,
        shopify: ShopifySource,
        store: OperationsStore,
        tills: Optional[OperationsStore] = None,
        terminal: Optional[TerminalRefundSource] = None,
    ):
        self.shopify, self.store, self.tills, self.terminal = shopify, store, tills or store, terminal
    def preview(self,order_id,store_code=None,invoice_no=None):
        order,verified_invoice,resolved_store=self._load_order(order_id,store_code,invoice_no)
        try:
            total=paise_from_decimal(order['total_price'])
        except (KeyError,TypeError,ValueError) as exc:
            raise OperationsError('Shopify order has an invalid or missing total') from exc
        sales=[t for t in order.get('transactions',[]) if t.get('kind')=='sale' and t.get('status')=='success']
        gateways = {str(transaction.get("gateway") or "") for transaction in sales}
        payment_gateway = next(iter(gateways)) if len(gateways) == 1 else None
        captured=sum(paise_from_decimal(t['amount']) for t in sales)
        previous_refunds=order.get('refunds',[])
        refunded_paise=sum(
            paise_from_decimal(transaction.get('amount','0'))
            for refund in previous_refunds
            for transaction in refund.get('transactions',[])
            if transaction.get('kind')=='refund' and transaction.get('status')=='success'
        )
        refunded_quantities={}
        for refund in previous_refunds:
            for line in refund.get('refund_line_items',[]):
                key=str(line.get('line_item_id'))
                refunded_quantities[key]=refunded_quantities.get(key,0)+int(line.get('quantity',0))
        lines=[]
        for line in order.get('line_items',[]):
            quantity=int(line.get('quantity',0))
            refunded=refunded_quantities.get(str(line.get('id')),0)
            discount=paise_from_decimal(line.get('total_discount','0'))
            tax=sum(paise_from_decimal(item.get('price','0')) for item in line.get('tax_lines',[]))
            line_total=paise_from_decimal(line.get('price','0'))*quantity-discount
            remaining=max(0,quantity-refunded)
            amount_due=(
                line_total-line_total*refunded//quantity
                if quantity > 0 else 0
            )
            tax_due=(tax-tax*refunded//quantity) if quantity > 0 else 0
            properties={
                item.get("name"): item.get("value")
                for item in line.get("properties",[])
                if isinstance(item,dict)
            }
            lines.append({
                'line_item_id':str(line.get('id')),
                'title':str(line.get('title') or line.get('name') or line.get('id')),
                'quantity':quantity,
                'refunded_quantity':refunded,
                'remaining_quantity':remaining,
                'discount_paise':discount,
                'tax_paise':tax,
                'remaining_refundable_paise':amount_due,
                'remaining_tax_paise':tax_due,
                'line_refundable_paise':line_total,
                'line_type':properties.get("line_type"),
                'restockable':properties.get("line_type")=="frame",
            })
        reasons=[]
        if order.get('cancelled_at') is not None:
            reasons.append('cancelled orders cannot be refunded')
        if total<=0 or captured<=0 or captured!=total:
            reasons.append('order is not fully paid')
        if order.get('financial_status') not in (None,'paid','partially_refunded'):
            reasons.append('Shopify order has no remaining refundable balance')
        remaining_refundable=max(0,captured-refunded_paise)
        if remaining_refundable <= 0:
            reasons.append('the original payment has already been fully refunded')
        if not lines or not any(line['remaining_quantity'] > 0 for line in lines):
            reasons.append('no refundable line quantities remain')
        available_tills = self.tills.open_tills(resolved_store) if payment_gateway == "cash" else []
        if payment_gateway == "cash":
            if not available_tills:
                reasons.append('cash refund requires an open till session for this store')
            capability = 'Cash refund requires a selected open till; the till movement is local reconciliation, not a payment-provider operation.'
        elif payment_gateway == "razorpay_terminal":
            if self.terminal is None or len(sales) != 1 or not sales[0].get("authorization"):
                reasons.append('the captured terminal transaction cannot be safely refunded by the configured payment adapter')
            capability = 'A simulated terminal refund is requested before the Shopify refund record; this is not real money movement.'
        else:
            reasons.append('no supported payment-provider refund operation is configured for this order')
            capability = 'No supported payment-provider refund operation is configured.'
        return {
            'order_id':str(order['id']),
            'invoice_no':verified_invoice,
            'name':order.get('name'),
            'created_at':order.get('created_at'),
            'customer_name':(order.get('customer') or {}).get('name'),
            'store_code':resolved_store,
            'financial_status':order.get('financial_status'),
            'total_price':str(order['total_price']),
            'total_paise':total,
            'refunded_paise':refunded_paise,
            'remaining_refundable_paise':remaining_refundable,
            'refund_count':len(previous_refunds),
            'payment_gateway':payment_gateway,
            'available_tills':available_tills,
            'lines':lines,
            'previous_refunds':[
                {
                    'refund_id':str(refund.get('id')),
                    'created_at':refund.get('created_at'),
                    'amount_paise':sum(
                        paise_from_decimal(tx.get('amount','0'))
                        for tx in refund.get('transactions',[])
                        if tx.get('kind')=='refund'
                    ),
                    'tax_paise':sum(
                        paise_from_decimal(item.get('total_tax','0'))
                        for item in refund.get('refund_line_items',[])
                    ),
                }
                for refund in previous_refunds
            ],
            'refund_eligible':not reasons,
            'ineligible_reasons':reasons,
            'payment_capability':capability,
        }

    def _load_order(self,order_id,store_code=None,invoice_no=None):
        order_id=str(order_id)
        if not order_id.isascii() or not order_id.isdecimal():
            raise OperationsError('order_id must be a numeric Shopify order ID; look up the invoice first')
        order=self.shopify.get_order(int(order_id))
        if not isinstance(order,dict):
            raise OperationsError('Shopify returned an invalid order record')
        if str(order.get('id'))!=order_id:
            raise OperationsError('Shopify returned an order that does not match the requested order ID')
        for field in ('transactions','line_items'):
            if not isinstance(order.get(field),list):
                raise OperationsError('Shopify order response is missing a valid {}'.format(field))
        if not isinstance(order.get('refunds',[]),list) or not isinstance(order.get('note_attributes',[]),list):
            raise OperationsError('Shopify order response contains invalid refund or invoice details')
        if any(not isinstance(item,dict) for item in order['transactions']+order['line_items']+order.get('refunds',[])+order.get('note_attributes',[])):
            raise OperationsError('Shopify order response contains an invalid order entry')
        resolved_store=self._belongs(order,store_code)
        invoice_attributes=[
            str(attribute.get('value'))
            for attribute in order.get('note_attributes',[])
            if attribute.get('name')=='invoice_no' and attribute.get('value')
        ]
        if len(invoice_attributes)>1:
            raise OperationsError('Shopify order has ambiguous invoice-number attributes')
        recorded_invoice=invoice_attributes[0] if invoice_attributes else None
        if invoice_no is not None and recorded_invoice != invoice_no:
            raise OperationsError('invoice number does not match the associated Shopify order; verify the invoice/order record')
        for refund in order.get('refunds',[]):
            if not isinstance(refund.get('refund_line_items',[]),list) or not isinstance(refund.get('transactions',[]),list):
                raise OperationsError('Shopify order response contains invalid previous-refund details')
        if not isinstance(order.get('customer') or {},dict):
            raise OperationsError('Shopify order response contains invalid customer details')
        if 'total_price' not in order:
            raise OperationsError('Shopify order response is missing its total')
        return order,recorded_invoice,resolved_store

    def refund(self,order_id,store_code,operator,reason,till_session_id=None, items=None, idempotency_key=None, invoice_no=None, return_item_ids=None):
        if not isinstance(reason,str) or not reason.strip() or len(reason.strip())>500:
            raise OperationsError('a refund reason of at most 500 characters is required')
        reason=reason.strip()
        order_id=str(order_id)
        if not order_id.isdigit():
            raise OperationsError('order_id must be a numeric Shopify order ID; look up the invoice first')
        request_key=idempotency_key or ('full:'+order_id if items is None else 'partial:'+order_id+':'+json.dumps(sorted((str(x['line_item_id']),int(x['quantity'])) for x in items)))
        request_data=json.dumps({
            'order_id':order_id,'store_code':store_code,'reason':reason,
            'till_session_id':till_session_id,
            'items':None if items is None else sorted((str(x['line_item_id']),int(x['quantity'])) for x in items),
            'return_item_ids':sorted(str(item) for item in (return_item_ids or [])),
        },sort_keys=True,separators=(',',':'))
        request_hash=hashlib.sha256(request_data.encode('utf-8')).hexdigest()
        existing=self.store.refund_by_request_key(request_key)
        if existing:
            if existing.get('request_hash')!=request_hash or existing['order_id']!=order_id:
                raise OperationsError('idempotency key was already used for a different or unknown refund request')
            if invoice_no is not None and existing.get('invoice_no')!=invoice_no:
                raise OperationsError('invoice number does not match the refund associated with this idempotency key')
            return existing
        order,verified_invoice,resolved_store=self._load_order(order_id,store_code,invoice_no)
        store_code=resolved_store
        if order.get('cancelled_at') is not None:
            raise OperationsError('cancelled orders cannot be refunded')
        eligibility=self.preview(order_id,store_code,invoice_no)
        if not eligibility['refund_eligible']:
            raise OperationsError('; '.join(eligibility['ineligible_reasons']))
        sales=[t for t in order.get('transactions',[]) if t.get('kind')=='sale' and t.get('status')=='success']
        amount=sum(paise_from_decimal(t['amount']) for t in sales); total=paise_from_decimal(order['total_price'])
        if amount<=0 or amount!=total: raise OperationsError('order is not fully paid and eligible for a full refund')
        previously_refunded=sum(
            paise_from_decimal(transaction.get('amount','0'))
            for refund in order.get('refunds',[])
            for transaction in refund.get('transactions',[])
            if transaction.get('kind')=='refund' and transaction.get('status')=='success'
        )
        remaining_refundable=max(0,amount-previously_refunded)
        gateways = {str(transaction.get("gateway") or "") for transaction in sales}
        if len(gateways) != 1:
            raise OperationsError('order has multiple payment gateways; reconcile the provider transactions before refunding')
        gateway = next(iter(gateways))
        if gateway == "cash":
            if not till_session_id:
                raise OperationsError('cash refund requires an open till session')
            till = self.tills.till_get(till_session_id)
            if till.get("status") != "OPEN" or till.get("store_code") != store_code or str(till.get("location_id")) != str(order.get("location_id")):
                raise OperationsError('cash refund till must be open and belong to this order store')
        elif gateway != "razorpay_terminal" or self.terminal is None:
            raise OperationsError('no supported payment-provider refund operation is configured for this order')
        elif len(sales) != 1 or not sales[0].get("authorization"):
            raise OperationsError('captured terminal transaction is missing its provider charge reference')
        full_request=items is None
        requested=(
            {
                str(line['id']):int(line['quantity'])-sum(
                    int(previous.get('quantity',0))
                    for refund in order.get('refunds',[])
                    for previous in refund.get('refund_line_items',[])
                    if str(previous.get('line_item_id'))==str(line['id'])
                )
                for line in order['line_items']
            }
            if full_request else
            {str(x['line_item_id']):int(x['quantity']) for x in items}
        )
        requested={line_id:quantity for line_id,quantity in requested.items() if quantity > 0}
        return_item_ids={str(item) for item in (return_item_ids or [])}
        if not return_item_ids.issubset(requested):
            raise OperationsError('restock selections must be among the refunded line items')
        if not requested or any(q<=0 for q in requested.values()): raise OperationsError('refund item quantities must be positive')
        by_id={str(x['id']):x for x in order['line_items']}; prior={}
        for refund in order.get('refunds',[]):
            for line in refund.get('refund_line_items',[]): prior[str(line['line_item_id'])]=prior.get(str(line['line_item_id']),0)+int(line['quantity'])
        lines=[]; amount=tax=0
        for line_id,qty in requested.items():
            li=by_id.get(line_id)
            if not li: raise OperationsError('line item is not on this order')
            previous_quantity=prior.get(line_id,0)
            total_quantity=int(li['quantity'])
            if qty>total_quantity-previous_quantity: raise OperationsError('refund quantity exceeds remaining eligible quantity')
            gross=(
                (paise_from_decimal(li['price'])*total_quantity-paise_from_decimal(li.get('total_discount','0.00')))
                * (previous_quantity+qty)//total_quantity
                - (paise_from_decimal(li['price'])*total_quantity-paise_from_decimal(li.get('total_discount','0.00')))
                * previous_quantity//total_quantity
                if 'price' in li else 0
            )
            line_tax_total=sum(paise_from_decimal(x['price']) for x in li.get('tax_lines',[]))
            line_tax=line_tax_total*(previous_quantity+qty)//total_quantity-line_tax_total*previous_quantity//total_quantity
            props={p.get('name'):p.get('value') for p in li.get('properties',[])}; frame=props.get('line_type')=='frame'
            if line_id in return_item_ids and not frame:
                raise OperationsError('only eligible frame items can be marked as physically returned')
            restock=frame and line_id in return_item_ids
            lines.append({'line_item_id':li['id'],'quantity':qty,'restock_type':'return' if restock else 'no_restock',**({'location_id':order['location_id']} if restock else {})})
            amount+=gross; tax+=line_tax
        if full_request:
            amount=remaining_refundable
        if amount <= 0 or amount > remaining_refundable:
            raise OperationsError('refund amount exceeds the remaining captured payment balance')
        row,created=self.store.refund_create(order_id,store_code,operator,reason,amount,tax,request_key,lines,request_hash,verified_invoice)
        if not created: return row
        tx=sales[0]
        if gateway == "razorpay_terminal":
            try:
                provider_result = self.terminal.refund(
                    str(tx["authorization"]), amount, reason, request_key,
                )
            except HttpRequestError as exc:
                status = "REFUND_UNKNOWN" if exc.status is None or exc.status >= 500 else "REFUND_FAILED"
                return self.store.refund_update(
                    row["refund_id"], status,
                    error="payment-provider refund outcome requires reconciliation: {}".format(exc),
                    audit_action="PAYMENT_REFUND_RESULT_UNKNOWN" if status == "REFUND_UNKNOWN" else "PAYMENT_REFUND_FAILED",
                    audit_actor=operator,
                    audit_reason=reason,
                )
            provider_refund_id = provider_result.get("refund_id")
            if (
                provider_result.get("status") != "processed"
                or not isinstance(provider_refund_id, str)
                or not provider_refund_id
                or provider_result.get("charge_id") != tx["authorization"]
                or isinstance(provider_result.get("amount_paise"), bool)
                or provider_result.get("amount_paise") != amount
            ):
                return self.store.refund_update(
                    row["refund_id"], "REFUND_UNKNOWN",
                    error="payment-provider refund response was incomplete; Shopify refund was not created",
                    audit_action="PAYMENT_REFUND_RESULT_UNKNOWN",
                    audit_actor=operator,
                    audit_reason=reason,
                )
            row = self.store.refund_update(
                row["refund_id"], "PENDING", terminal_refund_id=provider_refund_id,
                error=None, audit_action="PAYMENT_REFUND_PROCESSED",
                audit_actor=operator, audit_reason=reason,
            )
        payload={'refund_line_items':lines,'transactions':[{'kind':'refund','amount':decimal_string_from_paise(amount),'gateway':tx.get('gateway'),'parent_id':tx.get('id')}],'note':('Full refund: ' if full_request else 'Partial refund: ')+reason,'note_attributes':[{'name':'credit_note_no','value':row['credit_note']},{'name':'reason_code','value':reason}]}
        try: remote=self.shopify.create_refund(int(order_id),payload)
        except HttpRequestError:
            error = 'Shopify refund outcome unknown'
            if row.get("terminal_refund_id"):
                error = 'Payment-provider refund {} succeeded; Shopify refund record requires reconciliation'.format(row["terminal_refund_id"])
            return self.store.refund_update(row['refund_id'],'REFUND_UNKNOWN',error=error,audit_action='REFUND_OUTCOME_UNKNOWN',audit_actor=operator,audit_reason=reason)
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
        recorded_store=attrs.get('store_code')
        available=self.shopify.list_locations()
        if not isinstance(available,list) or any(not isinstance(location,dict) for location in available):
            raise OperationsError('Shopify returned invalid store-location details; verify the order with support')
        locations=[x for x in available if str(x.get('id'))==str(order.get('location_id'))]
        if len(locations)!=1:
            raise OperationsError('order location does not belong to a known store')
        location_store=(locations[0].get('eluno') or {}).get('store_code')
        if not recorded_store or recorded_store != location_store:
            raise OperationsError('order location does not belong to this store')
        if store is not None and recorded_store != store:
            raise OperationsError('order does not belong to this store')
        return str(recorded_store)
