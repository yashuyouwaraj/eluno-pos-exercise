from pathlib import Path
import sqlite3, tempfile, threading, unittest
from eluno_pos.business_time import bengaluru_business_date
from eluno_pos.operations import OperationsError, OperationsStore, RefundService
from eluno_pos.adapters.http import HttpRequestError

def order(refunds=None):
    return {'id':1,'location_id':7,'total_price':'150.00','refunds':refunds or [],'note_attributes':[{'name':'store_code','value':'HSR'}],
      'transactions':[{'id':9,'kind':'sale','status':'success','gateway':'razorpay_terminal','authorization':'charge-1','amount':'150.00'}],
      'line_items':[{'id':10,'quantity':1,'price':'100.00','total_discount':'0.00','tax_lines':[{'price':'10.00'}],'properties':[{'name':'line_type','value':'frame'}]}, {'id':11,'quantity':1,'price':'50.00','total_discount':'0.00','tax_lines':[{'price':'5.00'}],'properties':[{'name':'line_type','value':'lens'}]}]}
class Shopify:
 def __init__(self, outcome=None): self.o=order();self.calls=[];self.outcome=outcome
 def get_order(self,x): return self.o
 def create_refund(self,x,p): self.calls.append(p); 
 def list_locations(self): return [{'id':7,'eluno':{'store_code':'HSR'}}]

class Terminal:
 def __init__(self,outcome=None):self.calls=[];self.outcome=outcome
 def refund(self,charge_id,amount_paise,reason,idempotency_key):
  self.calls.append((charge_id,amount_paise,reason,idempotency_key))
  if isinstance(self.outcome,Exception):raise self.outcome
  return self.outcome or {'refund_id':'terminal-refund-1','charge_id':charge_id,'amount_paise':amount_paise,'status':'processed'}

class OpsTests(unittest.TestCase):
 def setUp(self): self.t=tempfile.TemporaryDirectory();self.store=OperationsStore(Path(self.t.name)/'x.db')
 def tearDown(self):self.t.cleanup()
 def test_till_cash_math_persistence_and_exclusion_by_caller(self):
  s=self.store.open_till('HSR','7','A',1000)
  with self.assertRaises(OperationsError):self.store.open_till('HSR','7','B',0)
  self.store.movement(s['session_id'],'cash_sale',500,'A',ref='sale');self.store.movement(s['session_id'],'paid_out',200,'A','tea','out')
  closed=self.store.close_till(s['session_id'],'A',1300);self.assertEqual(1300,closed['expected_paise']);self.assertEqual(0,closed['variance_paise'])
 def test_open_tills_lists_only_live_sessions_for_requested_store(self):
  hs=self.store.open_till('HSR','7','A',100)
  self.store.open_till('MM','8','B',200)
  self.assertEqual([hs['session_id']],[t['session_id'] for t in self.store.open_tills('HSR')])
 def test_sample_tills_reconcile_exactly_and_fixture_import_is_idempotent(self):
  fixture=Path(__file__).resolve().parents[1]/'starter-kit'/'data'/'till_sessions_sample.json'
  first=self.store.load_till_fixture(fixture)
  self.assertEqual({'imported':2,'skipped_existing':0},first)
  jn=self.store.till_get('TILL-JN-2026-10-06')
  self.assertEqual(300000,jn['opening_float_paise'])
  self.assertEqual(14496000,sum(m['amount_paise'] for m in jn['movements'] if m['type']=='cash_sale'))
  self.assertEqual(14876000,jn['expected_paise'])
  self.assertEqual(14851000,jn['counted_cash_paise'])
  self.assertEqual(-25000,jn['variance_paise'])
  self.assertEqual('Short; manager to review.',jn['review_note'])
  self.assertEqual('REVIEW_REQUIRED',jn['review_status'])
  self.assertTrue(jn['is_fixture'])
  vc=self.store.till_get('TILL-VC-2026-10-18')
  self.assertEqual(0,vc['variance_paise'])
  self.assertEqual('CLEAR',vc['review_status'])
  self.assertEqual({'imported':0,'skipped_existing':2},self.store.load_till_fixture(fixture))
  with self.store.session() as connection:
   self.assertEqual(5,connection.execute('SELECT COUNT(*) FROM cash_movements WHERE session_id=?',(jn['session_id'],)).fetchone()[0])
   connection.execute("UPDATE till_sessions SET review_note='Local review preserved' WHERE session_id=?",(jn['session_id'],))
  self.store.load_till_fixture(fixture)
  self.assertEqual('Local review preserved',self.store.till_get(jn['session_id'])['review_note'])
 def test_admin_review_is_audited_and_closed_till_cannot_receive_movements(self):
  till=self.store.open_till('HSR','7','cashier',1000)
  closed=self.store.close_till(till['session_id'],'closer',900,'Short count')
  reviewed=self.store.review_till(till['session_id'],'admin','Counted and reviewed')
  self.assertEqual('REVIEWED',reviewed['review_status'])
  self.assertEqual('Counted and reviewed',reviewed['review_note'])
  self.assertEqual(-100,reviewed['variance_paise'])
  self.assertEqual('closer',reviewed['closed_by'])
  with self.assertRaises(OperationsError):
   self.store.movement(till['session_id'],'paid_in',100,'cashier','change','after-close')
  self.assertEqual('TILL_REVIEWED',self.store.audit_events(till['session_id'])[-1]['action'])
 def test_refund_payload_reverses_tax_restocks_frame_not_lens_and_is_idempotent(self):
  shop=Shopify();shop.create_refund=lambda x,p: shop.calls.append(p) or {'id':99}
  terminal=Terminal();svc=RefundService(shop,self.store,terminal=terminal);r=svc.refund(1,'HSR','A','CHANGED',return_item_ids=['10']);again=svc.refund(1,'HSR','A','CHANGED',return_item_ids=['10'])
  self.assertEqual('COMPLETED',r['status']);self.assertEqual(r['refund_id'],again['refund_id']);self.assertEqual(1,len(shop.calls));self.assertEqual('return',shop.calls[0]['refund_line_items'][0]['restock_type']);self.assertEqual('no_restock',shop.calls[0]['refund_line_items'][1]['restock_type']);self.assertEqual(1500,r['tax_paise'])
  self.assertEqual('terminal-refund-1',r['terminal_refund_id']);self.assertEqual(1,len(terminal.calls))
 def test_wrong_store_and_unknown_refund_are_not_replayed(self):
  shop=Shopify();svc=RefundService(shop,self.store,terminal=Terminal())
  with self.assertRaises(OperationsError):svc.refund(1,'OTHER','A','x')
 def test_existing_refund_database_migrates_invoice_column_without_losing_rows(self):
  path=Path(self.t.name)/'legacy-refunds.db'
  connection=sqlite3.connect(str(path))
  connection.execute("""CREATE TABLE refunds(
   refund_id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,order_id TEXT NOT NULL,
   status TEXT NOT NULL,store_code TEXT NOT NULL,operator TEXT NOT NULL,reason TEXT NOT NULL,
   credit_note TEXT NOT NULL,amount_paise INTEGER NOT NULL,tax_paise INTEGER NOT NULL,
   lines_json TEXT NOT NULL DEFAULT '[]',shopify_refund_id TEXT,error TEXT,
   created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
   cash_till_id TEXT,cash_post_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED',request_hash TEXT
  )""")
  connection.execute("INSERT INTO refunds(refund_id,request_key,order_id,status,store_code,operator,reason,credit_note,amount_paise,tax_paise,created_at,updated_at) VALUES ('r1','k1','99','COMPLETED','HSR','A','x','cn1',100,10,'now','now')")
  connection.commit();connection.close()
  migrated=OperationsStore(path)
  row=migrated.refund_get('r1')
  self.assertEqual('99',row['order_id']);self.assertIsNone(row['invoice_no'])
 def test_partial_refunds_track_quantities_and_restock_by_line_type(self):
  shop=Shopify();shop.o['line_items'][0]['quantity']=2;shop.o['line_items'][0]['price']='100.00';shop.o['line_items'][0]['tax_lines']=[{'price':'18.00'}];shop.o['line_items'][1]['price']='50.00';shop.o['line_items'][1]['tax_lines']=[{'price':'2.50'}];shop.o['total_price']='250.00';shop.o['transactions'][0]['amount']='250.00';shop.create_refund=lambda x,p: shop.calls.append(p) or {'id':99}
  terminal=Terminal();svc=RefundService(shop,self.store,terminal=terminal);one=svc.refund(1,'HSR','A','x',items=[{'line_item_id':10,'quantity':1}],idempotency_key='one',return_item_ids=['10'])
  self.assertEqual(900,one['tax_paise']);self.assertEqual('return',shop.calls[0]['refund_line_items'][0]['restock_type'])
  shop.o['refunds']=[{'refund_line_items':shop.calls[0]['refund_line_items'],'transactions':[]}]
  two=svc.refund(1,'HSR','A','x',items=[{'line_item_id':11,'quantity':1}],idempotency_key='two')
  self.assertEqual('no_restock',shop.calls[1]['refund_line_items'][0]['restock_type']);self.assertNotEqual(one['refund_id'],two['refund_id'])
  shop.o['refunds'].append({'refund_line_items':shop.calls[1]['refund_line_items'],'transactions':[]})
  with self.assertRaises(OperationsError):svc.refund(1,'HSR','A','x',items=[{'line_item_id':11,'quantity':1}],idempotency_key='three')
 def test_partial_refund_idempotency_returns_same_record(self):
  shop=Shopify();shop.create_refund=lambda x,p: shop.calls.append(p) or {'id':99};svc=RefundService(shop,self.store,terminal=Terminal())
  first=svc.refund(1,'HSR','A','x',items=[{'line_item_id':10,'quantity':1}],idempotency_key='same',return_item_ids=['10'])
  second=svc.refund(1,'HSR','A','x',items=[{'line_item_id':10,'quantity':1}],idempotency_key='same',return_item_ids=['10'])
  self.assertEqual(first['refund_id'],second['refund_id']);self.assertEqual(1,len(shop.calls))
  with self.assertRaisesRegex(OperationsError,'different or unknown refund request'):
   svc.refund(1,'HSR','A','different reason',items=[{'line_item_id':10,'quantity':1}],idempotency_key='same')
  with self.assertRaisesRegex(OperationsError,'different or unknown refund request'):
   svc.refund(1,'HSR','A','x',items=[{'line_item_id':11,'quantity':1}],idempotency_key='same')
  self.assertEqual(1,len(shop.calls))
 def test_terminal_failure_is_unknown_and_never_creates_shopify_refund(self):
  shop=Shopify();terminal=Terminal(HttpRequestError(503,'terminal unavailable'))
  svc=RefundService(shop,self.store,terminal=terminal)
  first=svc.refund(1,'HSR','A','x',idempotency_key='terminal-unknown')
  replay=svc.refund(1,'HSR','A','x',idempotency_key='terminal-unknown')
  self.assertEqual('REFUND_UNKNOWN',first['status'])
  self.assertEqual(first['refund_id'],replay['refund_id'])
  self.assertIn('payment-provider refund outcome',first['error'])
  self.assertEqual([],shop.calls)
  self.assertEqual(1,len(terminal.calls))
 def test_shopify_failure_after_terminal_refund_is_not_reported_as_completed_or_retried(self):
  shop=Shopify()
  def fail_create(order_id,payload):
   shop.calls.append(payload)
   raise HttpRequestError(503,'Shopify unavailable')
  shop.create_refund=fail_create
  terminal=Terminal();svc=RefundService(shop,self.store,terminal=terminal)
  first=svc.refund(1,'HSR','A','x',idempotency_key='provider-before-shopify')
  replay=svc.refund(1,'HSR','A','x',idempotency_key='provider-before-shopify')
  self.assertEqual('REFUND_UNKNOWN',first['status'])
  self.assertEqual('terminal-refund-1',first['terminal_refund_id'])
  self.assertIn('succeeded',first['error'])
  self.assertEqual(first['refund_id'],replay['refund_id'])
  self.assertEqual(1,len(terminal.calls))
  self.assertEqual(1,len(shop.calls))
 def test_cash_refund_requires_an_open_store_till(self):
  shop=Shopify();shop.o['transactions'][0]['gateway']='cash'
  svc=RefundService(shop,self.store)
  with self.assertRaisesRegex(OperationsError,'open till session'):
   svc.refund(1,'HSR','A','cash refund')
 def test_close_and_movement_are_atomic_and_duplicate_close_fails(self):
  s=self.store.open_till('HSR','7','A',0); errors=[]
  def close():
   try:self.store.close_till(s['session_id'],'A',0)
   except OperationsError as e:errors.append(e)
  def move():
   try:self.store.movement(s['session_id'],'paid_in',1,'A','change','r')
   except OperationsError: pass
  threads=[threading.Thread(target=close),threading.Thread(target=move)]
  [x.start() for x in threads];[x.join() for x in threads]
  with self.assertRaises(OperationsError):self.store.close_till(s['session_id'],'A',0)
 def test_cash_variance_alert_strictly_exceeds_two_hundred_rupees(self):
  cases=((120000,None),(80000,None),(120001,20001),(79999,-20001))
  for counted,expected_variance in cases:
   with self.subTest(counted_paise=counted):
    till=self.store.open_till('HSR','7','Finance',100000)
    closed=self.store.close_till(till['session_id'],'Finance',counted)
    alert=self.store.cash_variance_alert(closed)
    if expected_variance is None:
     self.assertIsNone(alert)
     continue
    self.assertEqual(expected_variance,alert['variance_paise'])
    self.assertEqual(100000,alert['expected_cash_paise'])
    self.assertEqual(counted,alert['counted_cash_paise'])
    self.assertEqual('HSR',alert['store_code'])
    self.assertEqual(till['session_id'],alert['session_id'])
    self.assertEqual(20000,alert['threshold_paise'])
    self.assertEqual(
     bengaluru_business_date(closed['closed_at']).isoformat(),
     alert['business_date'],
    )
 def test_cash_variance_alerts_are_indexed_by_bengaluru_close_date(self):
  till=self.store.open_till('HSR','7','Finance',100000)
  closed=self.store.close_till(till['session_id'],'Finance',120001)
  business_date=bengaluru_business_date(closed['closed_at'])
  alerts=self.store.cash_variance_alerts('7',business_date)
  self.assertEqual([till['session_id']],[alert['session_id'] for alert in alerts])
  self.assertEqual([],self.store.cash_variance_alerts('8',business_date))
 def test_pending_refund_and_cash_post_recovery_never_repeat_shopify(self):
  shop=Shopify();shop.o['transactions'][0]['gateway']='cash';shop.create_refund=lambda x,p: shop.calls.append(p) or {'id':99}; svc=RefundService(shop,self.store)
  till=self.store.open_till('HSR','7','A',0); r=svc.refund(1,'HSR','A','x',till['session_id'])
  self.assertEqual('POSTED',r['cash_post_status']);self.assertEqual(1,len(shop.calls))
  self.store.refund_update(r['refund_id'],'COMPLETED',cash_post_status='PENDING')
  recovered=svc.recover(r['refund_id']);self.assertEqual('POSTED',recovered['cash_post_status']);self.assertEqual(1,len(shop.calls))
  pending,_=self.store.refund_create(2,'HSR','A','x',1,0); unresolved=svc.recover(pending['refund_id']);self.assertEqual('REFUND_UNKNOWN',unresolved['status'])
 def test_audit_events_are_durable_and_capture_actor_reason_and_till_actions(self):
  till=self.store.open_till('HSR','7','A',100)
  self.store.movement(till['session_id'],'paid_out',10,'A','change','cash-1')
  self.store.close_till(till['session_id'],'A',90)
  events=self.store.audit_events(till['session_id'])
  self.assertEqual(['TILL_OPENED','TILL_MOVEMENT','TILL_CLOSED'],[e['action'] for e in events])
  self.assertEqual('A',events[1]['actor']);self.assertEqual('change',events[1]['reason']);self.assertTrue(events[0]['occurred_at'])
 def test_audit_write_failure_rolls_back_till_open(self):
  original=self.store._audit_in_session
  self.store._audit_in_session=lambda *args,**kwargs: (_ for _ in ()).throw(OperationsError('audit storage unavailable'))
  with self.assertRaisesRegex(OperationsError,'audit storage unavailable'):
   self.store.open_till('HSR','7','A',0)
  self.store._audit_in_session=original
  self.assertEqual([],self.store.tills_for('7','2026-10-26'))
