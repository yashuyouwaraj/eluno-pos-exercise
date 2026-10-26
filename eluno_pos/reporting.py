"""Read-only store-local day-end reporting in integer paise."""
from datetime import date
from typing import Any, Dict, List, Protocol
from .business_time import bengaluru_business_date
from .money import paise_from_decimal
from .operations import OperationsStore

class ReportError(ValueError): pass

class ShopifyReports(Protocol):
 def list_orders(self, **query) -> List[Dict[str,Any]]: ...
 def list_locations(self) -> List[Dict[str,Any]]: ...

class DayEndReportService:
 def __init__(self,shopify:ShopifyReports, operations:OperationsStore, sales=None): self.shopify,self.operations,self.sales=shopify,operations,sales
 def report(self,store_id,business_date:date):
  locations=[x for x in self.shopify.list_locations() if str(x.get('id'))==str(store_id)]
  if len(locations)!=1 or not locations[0].get('active',True): raise ReportError('store_id is not an active store')
  store=locations[0]; code=(store.get('eluno') or {}).get('store_code')
  if not code: raise ReportError('store_id is not a billing store')
  all_orders=self.shopify.list_orders(location_id=str(store_id),status='any',limit='250')
  all_orders=[o for o in all_orders if str(o.get('location_id'))==str(store_id)]
  m={'gross_sales_paise':0,'discounts_paise':0,'net_sales_before_refunds_paise':0,'taxable_sales_paise':0,'cgst_paise':0,'sgst_paise':0,'tax_sales_paise':0,'refund_amount_paise':0,'refund_tax_paise':0}; methods={}; refunds=[]; exceptions=[]; channels={}
  orders=[]
  for order in all_orders:
   if order.get('cancelled_at'): continue
   try: order_date=bengaluru_business_date(order.get('processed_at'))
   except (TypeError, ValueError, OverflowError):
    exceptions.append({'type':'invalid_order_timestamp','order_id':order.get('id'),'manual_investigation_required':True}); continue
   if order_date != business_date or order.get('financial_status') not in ('paid','partially_refunded','refunded'): continue
   captured=sum(paise_from_decimal(tx['amount']) for tx in order.get('transactions',[]) if tx.get('kind')=='sale' and tx.get('status')=='success')
   if captured != paise_from_decimal(order.get('total_price','0.00')):
    exceptions.append({'type':'payment_reconciliation','order_id':order.get('id'),'manual_investigation_required':True})
    continue
   orders.append(order)
  def channel_for(o):
   value={x.get('name'):x.get('value') for x in o.get('note_attributes',[])}.get('channel')
   if value not in ('walk_in','whatsapp_booked','online_pickup'):
    exceptions.append({'type':'unknown_channel','order_id':o.get('id'),'manual_investigation_required':True}); return 'unknown'
   return value
  def bucket(channel):
   return channels.setdefault(channel,{'completed_bill_count':0,'gross_sales_paise':0,'discounts_paise':0,'net_sales_before_refunds_paise':0,'refund_amount_paise':0})
  for o in orders:
   channel=channel_for(o); b=bucket(channel); b['completed_bill_count']+=1
   m['gross_sales_paise']+=paise_from_decimal(o['total_line_items_price']);m['discounts_paise']+=paise_from_decimal(o['total_discounts']);m['net_sales_before_refunds_paise']+=paise_from_decimal(o['total_price'])
   b['gross_sales_paise']+=paise_from_decimal(o['total_line_items_price']);b['discounts_paise']+=paise_from_decimal(o['total_discounts']);b['net_sales_before_refunds_paise']+=paise_from_decimal(o['total_price'])
   for li in o.get('line_items',[]):
    tax=sum(paise_from_decimal(t['price']) for t in li.get('tax_lines',[])); payable=paise_from_decimal(li['price'])*int(li.get('quantity',1))-paise_from_decimal(li.get('total_discount','0.00'));m['tax_sales_paise']+=tax;m['taxable_sales_paise']+=payable-tax
    for t in li.get('tax_lines',[]):
     if t.get('title')=='CGST':m['cgst_paise']+=paise_from_decimal(t['price'])
     elif t.get('title')=='SGST':m['sgst_paise']+=paise_from_decimal(t['price'])
   recorded_method={x.get('name'):x.get('value') for x in o.get('note_attributes',[])}.get('payment_method')
   for tx in o.get('transactions',[]):
    if tx.get('kind')=='sale' and tx.get('status')=='success':
     gateway=tx.get('gateway')
     payment_method=recorded_method if recorded_method in ('cash','card','upi') else gateway if gateway in ('cash','card','upi') else 'unknown'
     methods[payment_method]=methods.get(payment_method,0)+paise_from_decimal(tx['amount'])
  for o in all_orders:
   for rf in o.get('refunds',[]):
    stamp=rf.get('created_at')
    try: refund_date=bengaluru_business_date(stamp)
    except (TypeError, ValueError, OverflowError):
     exceptions.append({'type':'invalid_refund_timestamp','refund_id':rf.get('id'),'manual_investigation_required':True}); continue
    if refund_date != business_date: continue
    amt=sum(paise_from_decimal(t['amount']) for t in rf.get('transactions',[]) if t.get('kind')=='refund'); tax=sum(paise_from_decimal(x['total_tax']) for x in rf.get('refund_line_items',[]));m['refund_amount_paise']+=amt;m['refund_tax_paise']+=tax;bucket(channel_for(o))['refund_amount_paise']+=amt;refunds.append(rf.get('id'))
  m['net_sales_after_refunds_paise']=m['net_sales_before_refunds_paise']-m['refund_amount_paise'];m['net_tax_after_refunds_paise']=m['tax_sales_paise']-m['refund_tax_paise']
  tills=self.operations.tills_for(str(store_id),business_date.isoformat())
  for t in tills:
   if t['status']=='OPEN': exceptions.append({'type':'open_till','session_id':t['session_id'],'manual_investigation_required':True})
  for r in self.operations.refunds_for(code):
   if r['status']!='COMPLETED' or r.get('cash_post_status')=='PENDING': exceptions.append({'type':'refund_reconciliation','refund_id':r['refund_id'],'status':r['status'],'manual_investigation_required':True})
  if self.sales:
   for s in self.sales.sales_for(code, business_date):
    if s['status'] in ('PAYMENT_UNKNOWN','ORDER_UNKNOWN','ORDER_FAILED'):
     exceptions.append({'type':'checkout_reconciliation','sale_id':s['sale_id'],'status':s['status'],'manual_investigation_required':True})
    elif s['status']=='COMPLETED' and s.get('payment_method')=='cash' and s.get('cash_post_status')=='PENDING':
     exceptions.append({'type':'cash_sale_reconciliation','sale_id':s['sale_id'],'till_session_id':s.get('till_session_id'),'amount_paise':s['amount_paise'],'manual_investigation_required':True})
  return {'store_id':str(store_id),'store_code':code,'business_date':business_date.isoformat(),'completed_bill_count':len(orders),'metrics':m,'payment_methods_paise':methods,'channels':channels,'refund_count':len(refunds),'tills':tills,'cash_variance_alerts':self.operations.cash_variance_alerts(str(store_id),business_date),'cash_sales_association':'recorded locally: completed cash checkout retains its till-session reference and posts a cash_sale movement with the sale ID reference','report_completeness':'bounded: mock order list has no pagination cursor','exceptions':exceptions}
