from datetime import date
from pathlib import Path
import tempfile, unittest
from eluno_pos.business_time import bengaluru_business_date, bengaluru_today

from eluno_pos.anomalies import AnomalyService, BillingAnomalyDetector
from eluno_pos.operations import OperationsStore
from eluno_pos.models import Prescription

class Shop:
 def list_locations(self): return [{'id':7,'active':True,'eluno':{'store_code':'HSR'}}]
 def list_orders(self,**q): return self.orders

class Sales:
 def __init__(self, rows): self.rows=rows
 def sales_for(self, store, day): return self.rows

class AnomalyTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.ops=OperationsStore(Path(self.tmp.name)/'state.db');self.shop=Shop();self.shop.orders=[];self.svc=AnomalyService(self.shop,self.ops)
 def tearDown(self): self.tmp.cleanup()
 def test_normal_and_missing_data_are_stable(self):
  self.shop.orders=[{'id':1,'location_id':7,'processed_at':'2026-10-26T10:00:00+05:30','financial_status':'paid','total_price':'10.00','total_line_items_price':'10.00','total_discounts':'0.00','transactions':[{'kind':'sale','status':'success','amount':'10.00'}]}]
  first=self.svc.report('7',date(2026,10,26));second=self.svc.report('7',date(2026,10,26))
  self.assertEqual([],first['flags']);self.assertEqual(first,second)
 def test_payment_discount_and_repeated_refund_flags(self):
  business_date=bengaluru_today()
  self.shop.orders=[{'id':1,'location_id':7,'processed_at':business_date.isoformat()+'T10:00:00+05:30','financial_status':'paid','total_price':'10.00','total_line_items_price':'5.00','total_discounts':'6.00','transactions':[{'kind':'sale','status':'success','amount':'9.00'}]}]
  for key in ('one','two'):
   row,_=self.ops.refund_create('1','HSR','staff','reason',100,0,key)
   self.ops.refund_update(row['refund_id'],'REFUND_UNKNOWN')
  rules={f['rule'] for f in self.svc.report('7',business_date)['flags']}
  self.assertTrue({'ORDER_PAYMENT_TOTAL_MISMATCH','INVALID_RECORDED_DISCOUNT','UNRESOLVED_REFUND','REPEATED_UNRESOLVED_REFUND'} <= rules)
 def test_invalid_store_is_rejected(self):
  with self.assertRaises(ValueError): self.svc.report('99',date(2026,10,26))
 def test_cash_variance_over_threshold_is_a_finance_anomaly(self):
  till=self.ops.open_till('HSR','7','Finance',100000)
  closed=self.ops.close_till(till['session_id'],'Finance',120001)
  business_date=bengaluru_business_date(closed['closed_at'])
  flags=self.svc.report('7',business_date)['flags']
  alert=next(flag for flag in flags if flag['rule']=='CASH_VARIANCE_OVER_200')
  self.assertEqual(20001,alert['variance_paise'])
  self.assertEqual(100000,alert['expected_cash_paise'])
  self.assertEqual(120001,alert['counted_cash_paise'])
  self.assertEqual('HSR',alert['store_code'])
  self.assertEqual(till['session_id'],alert['session_id'])
  self.assertEqual(business_date.isoformat(),alert['business_date'])
 def test_pending_cash_sale_and_refund_postings_are_anomalies(self):
  today=bengaluru_today()
  sale={'sale_id':'sale_pending_cash','order_id':'1','status':'COMPLETED','payment_method':'cash','cash_post_status':'PENDING','till_session_id':'till_1','amount_paise':12500,'updated_at':'2026-10-26T10:00:00+05:30'}
  refund,_=self.ops.refund_create('1','HSR','staff','reason',500,0,'cash-refund-pending')
  self.ops.refund_update(refund['refund_id'],'COMPLETED',cash_till_id='till_1',cash_post_status='PENDING')
  svc=AnomalyService(self.shop,self.ops,Sales([sale]))
  flags=svc.report('7',today)['flags']
  sale_flag=next(flag for flag in flags if flag['rule']=='CASH_SALE_POSTING_PENDING')
  refund_flag=next(flag for flag in flags if flag['rule']=='CASH_REFUND_POSTING_PENDING')
  self.assertEqual('sale_pending_cash',sale_flag['sale_id'])
  self.assertEqual(12500,sale_flag['evidence']['amount_paise'])
  self.assertEqual(refund['refund_id'],refund_flag['refund_id'])
  self.assertEqual('till_1',refund_flag['till_session_id'])
 def test_local_checkout_total_and_configured_offer_mismatches(self):
  self.shop.orders=[{'id':9,'location_id':7,'processed_at':'2026-10-26T10:00:00+05:30','financial_status':'paid','total_price':'11.00','total_line_items_price':'12.00','total_discounts':'1.00','transactions':[]}]
  sale={'sale_id':'sale_9','order_id':'9','amount_paise':1000,'status':'COMPLETED','order_payload':'{"line_items":[{"total_discount":"2.00"}]}' }
  svc=AnomalyService(self.shop,self.ops,Sales([sale]))
  rules={x['rule'] for x in svc.report('7',date(2026,10,26))['flags']}
  self.assertIn('LOCAL_SHOPIFY_TOTAL_MISMATCH',rules)
  self.assertIn('CONFIGURED_OFFER_DISCOUNT_MISMATCH',rules)
 def test_billing_rules_cover_both_eyes_boundary_and_unknown_history(self):
  detector=BillingAnomalyDetector()
  def rx(ident, day, od, os, axes=(90,90)):
   return Prescription.from_data({'rx_id':ident,'customer_id':'1','phone':'+91','date':day,'valid_until':'2027-01-01','od':{'sph':od,'cyl':'0','axis':axes[0],'add':'2'},'os':{'sph':os,'cyl':'0','axis':axes[1],'add':'2'}})
  old=rx('old','2026-01-01','-1.00','-1.00'); current=rx('new','2026-02-01','-2.01','0.00',(91,95))
  flags=detector.flags(current,[old,current],{'lens_type':'single_vision'},[],False)
  rules=[x['rule'] for x in flags]
  self.assertIn('SPHERE_CHANGE_OVER_1D',rules);self.assertNotIn('AXIS_NOT_ON_5_DEGREE_GRID',rules)
  self.assertEqual('od',[x['eye'] for x in flags if x['rule']=='SPHERE_CHANGE_OVER_1D'][0])
  boundary=rx('boundary','2026-02-01','-2.00','-1.00')
  self.assertFalse(any(x['rule']=='SPHERE_CHANGE_OVER_1D' for x in detector.flags(boundary,[old,boundary],{'lens_type':'single_vision'},[],False)))
  missing=rx('missing','2026-02-01',None,'-1.00')
  self.assertFalse(any(x['rule']=='SPHERE_CHANGE_OVER_1D' for x in detector.flags(missing,[old,missing],{'lens_type':'single_vision'},[],False)))
 def test_sphere_history_tie_is_unknown_and_each_eye_is_compared(self):
  detector=BillingAnomalyDetector()
  def rx(ident, day, od, os):
   return Prescription.from_data({'rx_id':ident,'customer_id':'1','phone':'+91','date':day,'valid_until':'2027-01-01','od':{'sph':od,'cyl':'0','axis':90,'add':'2'},'os':{'sph':os,'cyl':'0','axis':90,'add':'2'}})
  old=rx('old','2026-01-01','-1.00','-1.00'); current=rx('new','2026-02-01','-2.25','0.50')
  eyes={x['eye'] for x in detector.flags(current,[old,current],{'lens_type':'single_vision'},[],False) if x['rule']=='SPHERE_CHANGE_OVER_1D'}
  self.assertEqual({'od','os'},eyes)
  tied=rx('tied','2026-01-01','-4.00','-4.00')
  self.assertFalse(any(x['rule']=='SPHERE_CHANGE_OVER_1D' for x in detector.flags(current,[old,tied,current],{'lens_type':'single_vision'},[],False)))
 def test_progressive_first_time_requires_complete_history(self):
  detector=BillingAnomalyDetector(); rx=Prescription.from_data({'rx_id':'new','customer_id':'1','phone':'+91','date':'2026-02-01','valid_until':'2027-01-01','od':{'sph':'-1','cyl':'0','axis':90,'add':'2'},'os':{'sph':'-1','cyl':'0','axis':90,'add':'2'}})
  self.assertFalse(any(x['rule']=='PROGRESSIVE_FOR_FIRST_TIME_WEARER' for x in detector.flags(rx,[rx],{'lens_type':'progressive'},[],False)))
  flags=detector.flags(rx,[rx],{'lens_type':'progressive'},[],True)
  self.assertIn('PROGRESSIVE_FOR_FIRST_TIME_WEARER',{x['rule'] for x in flags})
  prior=[{'customer':{'id':'1'},'line_items':[{'properties':[{'name':'lens_config','value':'progressive/1.60/standard'}]}]}]
  self.assertFalse(any(x['rule']=='PROGRESSIVE_FOR_FIRST_TIME_WEARER' for x in detector.flags(rx,[rx],{'lens_type':'progressive'},prior,True)))
