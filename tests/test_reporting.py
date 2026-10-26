from datetime import date
from pathlib import Path
import tempfile, unittest
from eluno_pos.business_time import bengaluru_business_date
from eluno_pos.operations import OperationsStore
from eluno_pos.reporting import DayEndReportService, ReportError

class Shop:
 def list_locations(self): return [{'id':7,'active':True,'eluno':{'store_code':'HSR'}}]
 def list_orders(self,**q): return self.orders

class ReportTests(unittest.TestCase):
 def setUp(self): self.t=tempfile.TemporaryDirectory();self.ops=OperationsStore(Path(self.t.name)/'x.db');self.shop=Shop();self.shop.orders=[];self.svc=DayEndReportService(self.shop,self.ops)
 def tearDown(self):self.t.cleanup()
 def test_empty_store_and_invalid_store(self):
  r=self.svc.report('7',date(2026,10,26));self.assertEqual(0,r['completed_bill_count']);self.assertIn('recorded locally',r['cash_sales_association'])
  with self.assertRaises(ReportError):self.svc.report('99',date(2026,10,26))
 def test_sales_refunds_tax_payments_and_date_store_filtering(self):
  base={'location_id':7,'processed_at':'2026-10-26T10:00:00+05:30','cancelled_at':None,'financial_status':'paid','total_line_items_price':'100.00','total_discounts':'10.00','total_price':'90.00','transactions':[{'kind':'sale','status':'success','gateway':'cash','amount':'90.00'}],'line_items':[{'price':'100.00','quantity':1,'total_discount':'10.00','tax_lines':[{'title':'CGST','price':'7.63'},{'title':'SGST','price':'7.63'}]}],'refunds':[{'id':1,'created_at':'2026-10-26T12:00:00+05:30','transactions':[{'kind':'refund','amount':'90.00'}],'refund_line_items':[{'total_tax':'15.26'}]}]}
  self.shop.orders=[base,dict(base,location_id=8),dict(base,processed_at='2026-10-25T23:00:00+05:30')]
  r=self.svc.report('7',date(2026,10,26));self.assertEqual(1,r['completed_bill_count']);self.assertEqual(10000,r['metrics']['gross_sales_paise']);self.assertEqual(18000,r['metrics']['refund_amount_paise']);self.assertEqual(9000,r['payment_methods_paise']['cash'])
 def test_refund_uses_its_own_date_and_invalid_timestamp_is_exception(self):
  old={'location_id':7,'processed_at':'2026-10-25T10:00:00+05:30','cancelled_at':None,'financial_status':'paid','total_line_items_price':'10.00','total_discounts':'0.00','total_price':'10.00','transactions':[],'line_items':[],'refunds':[{'id':2,'created_at':'2026-10-26T10:00:00+05:30','transactions':[{'kind':'refund','amount':'10.00'}],'refund_line_items':[]},{'id':3,'created_at':'bad','transactions':[{'kind':'refund','amount':'1.00'}],'refund_line_items':[]}]}
  self.shop.orders=[old];r=self.svc.report('7',date(2026,10,26));self.assertEqual(0,r['completed_bill_count']);self.assertEqual(1000,r['metrics']['refund_amount_paise']);self.assertTrue(any(x['type']=='invalid_refund_timestamp' for x in r['exceptions']))
 def test_channel_breakdown_keeps_unknown_and_refunds_separate(self):
  order={'id':1,'location_id':7,'processed_at':'2026-10-26T10:00:00+05:30','cancelled_at':None,'financial_status':'paid','total_line_items_price':'100.00','total_discounts':'10.00','total_price':'90.00','note_attributes':[{'name':'channel','value':'online_pickup'}],'transactions':[{'kind':'sale','status':'success','gateway':'razorpay_terminal','amount':'90.00'}],'line_items':[],'refunds':[]}
  unknown=dict(order,id=2,note_attributes=[])
  old=dict(order,id=3,processed_at='2026-10-25T10:00:00+05:30',refunds=[{'id':4,'created_at':'2026-10-26T11:00:00+05:30','transactions':[{'kind':'refund','amount':'90.00'}],'refund_line_items':[]}])
  self.shop.orders=[order,unknown,old];r=self.svc.report('7',date(2026,10,26))
  self.assertEqual(1,r['channels']['online_pickup']['completed_bill_count'])
  self.assertEqual(9000,r['channels']['online_pickup']['refund_amount_paise'])
  self.assertEqual(1,r['channels']['unknown']['completed_bill_count'])
  self.assertTrue(any(x['type']=='unknown_channel' and x['order_id']==2 for x in r['exceptions']))
 def test_day_end_includes_closed_till_variance_alert_for_business_date(self):
  till=self.ops.open_till('HSR','7','Finance',100000)
  closed=self.ops.close_till(till['session_id'],'Finance',120001)
  business_date=bengaluru_business_date(closed['closed_at'])
  report=self.svc.report('7',business_date)
  alert=report['cash_variance_alerts'][0]
  self.assertEqual(20001,alert['variance_paise'])
  self.assertEqual('HSR',alert['store_code'])
  self.assertEqual(till['session_id'],alert['session_id'])
  self.assertEqual(business_date.isoformat(),alert['business_date'])

 def test_day_end_reports_completed_cash_sale_with_pending_till_posting(self):
  class Sales:
   def sales_for(self, store_code, business_date):
    return [{
     'sale_id':'sale_pending_cash','status':'COMPLETED','payment_method':'cash',
     'cash_post_status':'PENDING','till_session_id':'till_1','amount_paise':12500,
    }]
  report=DayEndReportService(self.shop,self.ops,Sales()).report('7',date(2026,10,26))
  exception=next(item for item in report['exceptions'] if item['type']=='cash_sale_reconciliation')
  self.assertEqual('sale_pending_cash',exception['sale_id'])
  self.assertEqual('till_1',exception['till_session_id'])
  self.assertEqual(12500,exception['amount_paise'])

 def test_day_end_scopes_unresolved_checkouts_to_store_and_business_date(self):
  class Sales:
   def sales_for(self, store_code, business_date):
    return [{'sale_id':'sale_in_scope','status':'ORDER_UNKNOWN'}]
   def unresolved(self):
    return [{'sale_id':'sale_other_store_or_date','status':'ORDER_UNKNOWN'}]
  report=DayEndReportService(self.shop,self.ops,Sales()).report('7',date(2026,10,26))
  exceptions=[item for item in report['exceptions'] if item['type']=='checkout_reconciliation']
  self.assertEqual(['sale_in_scope'],[item['sale_id'] for item in exceptions])
