"""Read-only deterministic operational anomaly flags, not a trained ML/fraud model."""
from datetime import date
from decimal import Decimal
import json
from .business_time import bengaluru_business_date
from .models import Prescription
from .money import paise_from_decimal
from .reporting import ReportError

class BillingAnomalyDetector:
    """Pure, advisory billing-time rules; never changes an Rx or blocks a sale."""
    def flags(self, rx, history, lens, orders, complete):
        flags=[]; prior=[p for p in history if p.rx_id!=rx.rx_id and p.customer_id==rx.customer_id and p.phone==rx.phone and p.prescribed_on<rx.prescribed_on]
        if prior:
            latest=max(p.prescribed_on for p in prior); rows=[p for p in prior if p.prescribed_on==latest]
            if len(rows)==1:
                old=rows[0]
                for name,current,previous in (("od",rx.od,old.od),("os",rx.os,old.os)):
                    if current.sph is not None and previous.sph is not None and abs(current.sph-previous.sph)>Decimal('1.00'):
                        delta=abs(current.sph-previous.sph); flags.append(self._flag('sphere-change:{}:{}'.format(rx.rx_id,name),'SPHERE_CHANGE_OVER_1D','medium',{'rx_id':rx.rx_id,'previous_rx_id':old.rx_id,'eye':name},'Sphere changed by more than 1.00 D; ask the optometrist to confirm before billing.',{'current_sphere':str(current.sph),'previous_sphere':str(previous.sph),'change_diopters':str(delta)}))
        if lens.get('lens_type')=='progressive' and complete:
            mine=[o for o in orders if str((o.get('customer') or {}).get('id') or '')==rx.customer_id]
            progressive=any(any(any(p.get('name')=='lens_config' and str(p.get('value','')).split('/')[0]=='progressive' for p in line.get('properties',[])) for line in o.get('line_items',[])) for o in mine)
            if not progressive:
                flags.append(self._flag('progressive-first-wearer:'+rx.rx_id,'PROGRESSIVE_FOR_FIRST_TIME_WEARER','medium',{'rx_id':rx.rx_id,'customer_id':rx.customer_id},'No prior progressive lens order is present in complete available order history; confirm first-time-wearer counselling.',{'selected_lens_type':'progressive','reviewed_orders':len(mine),'order_history_complete':True}))
        return sorted(flags,key=lambda f:f['id'])
    @staticmethod
    def _flag(identifier,rule,severity,refs,explanation,evidence): return {'id':identifier,'rule':rule,'severity':severity,**refs,'explanation':explanation,'evidence':evidence}

class AnomalyService:
    def __init__(self, shopify, operations, sales=None, prescriptions=None): self.shopify,self.operations,self.sales,self.prescriptions=shopify,operations,sales,prescriptions; self.billing=BillingAnomalyDetector()
    def billing_flags(self, phone, rx_id, lens):
        if self.prescriptions is None: raise ReportError('billing anomaly lookup is not configured')
        history=[Prescription.from_data(item) for item in self.prescriptions.prescriptions_by_phone(phone)]
        selected=[p for p in history if p.rx_id==str(rx_id)]
        if len(selected)!=1: raise ReportError('prescription reference is missing or ambiguous')
        rx=selected[0]
        if any(p.customer_id!=rx.customer_id or p.phone!=rx.phone for p in history): raise ReportError('prescription history does not consistently belong to the customer')
        orders=self.shopify.list_orders(status='any',limit='250')
        complete=len(orders)<250
        return {'rx_id':rx.rx_id,'history_status':'complete_available_history' if complete else 'unknown_bounded_order_history','flags':self.billing.flags(rx,history,lens,orders,complete)}
    def report(self, store_id, business_date: date):
        locations=[x for x in self.shopify.list_locations() if str(x.get('id'))==str(store_id)]
        if len(locations)!=1 or not locations[0].get('active',True): raise ReportError('store_id is not an active store')
        code=(locations[0].get('eluno') or {}).get('store_code')
        if not code: raise ReportError('store_id is not a billing store')
        orders=[]
        for order in self.shopify.list_orders(location_id=str(store_id),status='any',limit='250'):
            if str(order.get('location_id')) != str(store_id):
                continue
            try:
                order_date = bengaluru_business_date(order.get('processed_at'))
            except (TypeError, ValueError, OverflowError):
                continue
            if order_date == business_date:
                orders.append(order)
        local=self.sales.sales_for(code,business_date) if self.sales else []
        by_order={str(s['order_id']):s for s in local if s.get('order_id')}
        flags=[]
        for order in orders:
            oid=str(order.get('id')); stamp=order.get('processed_at'); total=self._money(order.get('total_price'))
            paid=sum(self._money(t.get('amount')) or 0 for t in order.get('transactions',[]) if t.get('kind')=='sale' and t.get('status')=='success')
            if total is not None and order.get('financial_status')=='paid' and paid!=total:
                flags.append(self._flag('order-payment-total:'+oid,'ORDER_PAYMENT_TOTAL_MISMATCH','high',stamp,{'order_id':oid},'Successful Shopify sale transactions do not reconcile to the paid order total.',{'order_total_paise':total,'successful_sale_paise':paid}))
            gross,discount=self._money(order.get('total_line_items_price')),self._money(order.get('total_discounts'))
            if gross is None or discount is None:
                flags.append(self._flag('discount-data:'+oid,'INVALID_DISCOUNT_DATA','medium',stamp,{'order_id':oid},'Order discount fields are not valid monetary values.',{}))
            elif discount<0 or discount>gross:
                flags.append(self._flag('discount-range:'+oid,'INVALID_RECORDED_DISCOUNT','high',stamp,{'order_id':oid},'Recorded discount is outside the valid zero-to-gross range.',{'gross_paise':gross,'discount_paise':discount}))
            sale=by_order.get(oid)
            if sale:
                expected=self._payload_discount(sale)
                if total is not None and total!=int(sale['amount_paise']):
                    flags.append(self._flag('local-order-total:'+oid,'LOCAL_SHOPIFY_TOTAL_MISMATCH','high',stamp,{'sale_id':sale['sale_id'],'order_id':oid},"Stored checkout payable amount differs from Shopify's authoritative order total.",{'checkout_payable_paise':int(sale['amount_paise']),'shopify_total_paise':total}))
                if expected is not None and discount is not None and expected!=discount:
                    flags.append(self._flag('configured-offer-discount:'+oid,'CONFIGURED_OFFER_DISCOUNT_MISMATCH','medium',stamp,{'sale_id':sale['sale_id'],'order_id':oid},"Shopify discount differs from the checkout's persisted configured-offer result.",{'checkout_offer_discount_paise':expected,'shopify_discount_paise':discount}))
        if self.sales:
            for sale in local:
                if sale['status'] in ('PAYMENT_UNKNOWN','ORDER_UNKNOWN','ORDER_FAILED'):
                    flags.append(self._flag('unresolved-sale:'+sale['sale_id'],'UNRESOLVED_CHECKOUT','high',sale.get('updated_at'),{'sale_id':sale['sale_id']},'Checkout requires reconciliation; no external work was replayed.',{'status':sale['status']}))
                elif sale['status']=='COMPLETED' and sale.get('payment_method')=='cash' and sale.get('cash_post_status')=='PENDING':
                    flags.append(self._flag(
                        'cash-sale-posting-pending:'+sale['sale_id'],
                        'CASH_SALE_POSTING_PENDING',
                        'high',
                        sale.get('updated_at'),
                        {'sale_id':sale['sale_id'],'till_session_id':sale.get('till_session_id')},
                        'Completed cash sale has not been posted to its till session; reconcile the local cash movement.',
                        {'amount_paise':int(sale['amount_paise'])},
                    ))
        unresolved=[]
        for refund in self.operations.refunds_for(code):
            try:
                refund_date = bengaluru_business_date(refund.get('created_at'))
            except (TypeError, ValueError, OverflowError):
                continue
            if (refund['status'] != 'COMPLETED' or refund.get('cash_post_status') == 'PENDING') and refund_date == business_date:
                unresolved.append(refund)
        for refund in unresolved:
            if refund['status'] == 'COMPLETED' and refund.get('cash_post_status') == 'PENDING':
                flags.append(self._flag(
                    'cash-refund-posting-pending:'+refund['refund_id'],
                    'CASH_REFUND_POSTING_PENDING',
                    'high',
                    refund.get('updated_at'),
                    {'refund_id':refund['refund_id'],'order_id':refund['order_id'],'till_session_id':refund.get('cash_till_id')},
                    'Completed cash refund has not been posted to its till session; reconcile the local cash movement.',
                    {'amount_paise':int(refund['amount_paise'])},
                ))
            else:
                flags.append(self._flag('unresolved-refund:'+refund['refund_id'],'UNRESOLVED_REFUND','high',refund.get('updated_at'),{'refund_id':refund['refund_id'],'order_id':refund['order_id']},'Refund outcome requires reconciliation; it was not replayed.',{'status':refund['status']}))
        for oid in sorted({r['order_id'] for r in unresolved}):
            ids=sorted(r['refund_id'] for r in unresolved if r['order_id']==oid)
            if len(ids)>1: flags.append(self._flag('repeated-unresolved-refund:'+str(oid),'REPEATED_UNRESOLVED_REFUND','high',None,{'order_id':str(oid)},'More than one unresolved local refund record exists for the same order.',{'refund_ids':ids}))
        for alert in self.operations.cash_variance_alerts(str(store_id),business_date):
            flags.append({
                'id': alert['id'],
                'rule': 'CASH_VARIANCE_OVER_200',
                'severity': alert['severity'],
                'timestamp': alert['closed_at'],
                'store_code': alert['store_code'],
                'session_id': alert['session_id'],
                'business_date': alert['business_date'],
                'expected_cash_paise': alert['expected_cash_paise'],
                'counted_cash_paise': alert['counted_cash_paise'],
                'variance_paise': alert['variance_paise'],
                'threshold_paise': alert['threshold_paise'],
                'explanation': alert['explanation'],
            })
        return {'store_id':str(store_id),'store_code':code,'business_date':business_date.isoformat(),'detector':'deterministic operational rules; not an ML fraud model','report_completeness':'bounded: mock order list has no pagination cursor; only returned Shopify orders were checked','flags':sorted(flags,key=lambda f:f['id'])}
    @staticmethod
    def _money(value):
        try:return paise_from_decimal(value)
        except (TypeError,ValueError):return None
    @staticmethod
    def _payload_discount(sale):
        try:return sum(paise_from_decimal(x.get('total_discount','0.00')) for x in json.loads(sale['order_payload']).get('line_items',[]))
        except (TypeError,ValueError):return None
    @staticmethod
    def _flag(identifier,rule,severity,timestamp,refs,explanation,evidence):
        return {'id':identifier,'rule':rule,'severity':severity,'timestamp':timestamp,**refs,'explanation':explanation,'evidence':evidence}
