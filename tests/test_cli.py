import unittest
from datetime import date
from types import SimpleNamespace
from urllib.parse import parse_qs

from unittest.mock import patch

from eluno_pos.business_time import bengaluru_today
from eluno_pos.cli import ApiClient, ApiError, CashierCLI

class Api:
 def __init__(self): self.calls=[];self.base_url='http://127.0.0.1:8790'
 def health(self): self.calls.append(('health',))
 def request(self, method,path,body=None):
  self.calls.append((method,path,body))
  if path=='/health':return 200,{'ok':True,'service':'eluno-pos'}
  if path=='/api/catalog/variants?line_type=frame':
   return 200,{'variants':[{'variant_id':'1','product_title':'Test frame','variant_title':'Standard','price_paise':10000},{'variant_id':'91000001','product_title':'Seed frame','variant_title':'Classic','price_paise':10000}]}
  if path=='/api/catalog/variants?line_type=lens':
   return 200,{'variants':[{'variant_id':'91000002','product_title':'Test lens','variant_title':'Single vision','price_paise':10000,'lens_type':'single_vision','index':'1.60','coating':'standard'}]}
  if path.startswith('/api/offers?'):
   return 200,{'offers':[{'offer_id':'FESTIVE10','name':'Festive 10% on frames','stackable':False}]}
  if path=='/api/prescriptions':
   self.prescription_payload=body
   return 201,{'prescription':{'rx_id':'RX-new'},'confirmed_by':body['optometrist_id']}
  if 'customer-prescription' in path:
   phone=parse_qs(path.split('?',1)[1]).get('phone',[''])[0].replace('%2B','+')
   if phone in ('+919000000001','+917970629433'):return 200,{'customer':{'id':1,'first_name':'A','last_name':'B','phone':phone},'prescription':{'rx_id':'RX1','valid_until':'2027-01-01'}}
   raise ApiError('No customer was found for this phone number.',status=404,payload={'status':'customer_not_found'})
  if path=='/api/customers':
   first,*last=body['name'].split(' ',1)
   return 201,{'customer':{'id':42,'first_name':first,'last_name':last[0] if last else '','phone':body['phone']},'created':True}
  if path=='/':return 200,{'service':'eluno-pos'}
  if path=='/api/checkouts':return 201,{'status':'COMPLETED','sale_id':'sale_1','order_id':'99','invoice_no':'INV1'}
  if 'anomalies' in path:return 200,{'flags':[],'report_completeness':'bounded'}
  if path=='/api/system-status':return 200,{'components':[{'name':'Shopify adapter','status':'HEALTHY','explanation':'read-only check succeeded'},{'name':'Store-tool adapter','status':'HEALTHY','explanation':'read-only check succeeded'},{'name':'Payment terminal','status':'REACHABLE','explanation':'payment readiness was not tested'}]}
  if 'day-end' in path:return 200,{'completed_bill_count':0,'metrics':{'gross_sales_paise':0,'discounts_paise':0,'net_sales_before_refunds_paise':0,'refund_amount_paise':0},'channels':{},'exceptions':[],'report_completeness':'bounded'}
  return 200,{'status':'COMPLETED'}

class CliTests(unittest.TestCase):
 def make(self, answers, api=None):
  output=[]; items=iter(answers)
  return CashierCLI(api or Api(),lambda _:next(items),output.append),output
 def test_menu_exit_and_invalid_selection(self):
  cli,out=self.make(['x','0']);self.assertEqual(0,cli.run());self.assertTrue(any('Please enter' in x for x in out))
 def test_admin_catalog_workflow_creates_products_and_receives_frame_stock(self):
  class AdminApi(Api):
   def request(self,method,path,body=None):
    self.calls.append((method,path,body))
    if path=='/api/admin/catalog/products':
     self.product_payload=body
     return 201,{'product':{'id':90,'title':body['title'],'variants':[{'id':91,'sku':body['sku']}]}}
    if path=='/api/admin/catalog/frames':
     return 200,{'variants':[{'variant_id':91,'inventory_item_id':92,'product_title':'New Frame','variant_title':'Black','sku':'F-NEW'}]}
    if path=='/api/admin/locations':
     return 200,{'locations':[{'id':7,'name':'Eluno Jayanagar','store_code':'JN'}]}
    if path=='/api/admin/inventory/receipts':
     self.receipt_payload=body
     return 200,{'inventory_level':{'available':body['quantity']}}
    return super().request(method,path,body)
  api=AdminApi()
  cli,out=self.make(['1','New Frame','F-NEW','4999.00','S1','1','Black','TR90','52-18-140','y'],api)
  cli.add_catalog_product()
  self.assertEqual('frame',api.product_payload['line_type'])
  self.assertEqual('optical',api.product_payload['category'])
  self.assertTrue(api.product_payload['rx_able'])
  cli,out=self.make(['1','1','4','S1','GRN-001'],api)
  cli.receive_frame_stock()
  self.assertEqual({'inventory_item_id':92,'location_id':7,'quantity':4,'actor':'S1','reference':'GRN-001'},api.receipt_payload)
  self.assertTrue(any('now has 4 available' in line for line in out))
 def test_server_unavailable_is_clear(self):
  class Down(Api):
   def health(self): raise ApiError('Server unavailable: connection refused. Start it with: python scripts/start_local.py')
  cli,out=self.make([],Down());self.assertEqual(1,cli.run());self.assertIn('start_local.py',out[0])
 def test_customer_lookup_normalizes_mobile_number_before_api_call(self):
  api=Api();cli,out=self.make(['7970629433'],api)
  cli.find_customer()
  lookup=next(call[1] for call in api.calls if len(call)>1 and '/api/customer-prescription?' in call[1])
  self.assertIn('/api/customer-prescription?phone=%2B917970629433',lookup)
  self.assertIn('&business_date={}'.format(bengaluru_today().isoformat()),lookup)
  self.assertFalse(any('Business date' in line for line in out))
 def test_sale_displays_authoritative_priority_and_campaign_tags(self):
  class TaggedCustomer(Api):
   def request(self,method,path,body=None):
    if 'customer-prescription' in path:
     self.calls.append((method,path,body))
     return 200,{'customer':{
      'id':42,'phone':'+919000000001','first_name':'A','last_name':'B',
      'tags':['priority','campaign:festive_frames'],
     },'prescription':{'rx_id':'RX1'}}
    return super().request(method,path,body)
  api=TaggedCustomer();cli,out=self.make(['A B','9000000001'],api)
  cli._sale_customer(date(2026,10,27))
  text='\n'.join(out)
  self.assertIn('Verified Shopify customer flags (separate from sales channel):',text)
  self.assertIn('Priority: Yes',text)
  self.assertIn('Campaign tags: festive_frames',text)
  self.assertFalse(any('walk_in' in line for line in out))
 def test_sale_displays_no_priority_or_campaign_when_customer_has_no_tags(self):
  class UntaggedCustomer(Api):
   def request(self,method,path,body=None):
    if 'customer-prescription' in path:
     return 200,{'customer':{
      'id':42,'phone':'+919000000001','first_name':'A','last_name':'B','tags':[],
     },'prescription':{'rx_id':'RX1'}}
    return super().request(method,path,body)
  cli,out=self.make(['A B','9000000001'],UntaggedCustomer())
  cli._sale_customer(date(2026,10,27))
  self.assertTrue(any('Priority: No' in line for line in out))
  self.assertTrue(any('Campaign tags: none' in line for line in out))
 def test_sales_channel_is_printed_separately_from_customer_flags(self):
  cli,out=self.make(['JN','70000005','2026-10-27','1','','','S119','0'])
  cli.new_sale()
  self.assertTrue(any(line=='Sales channel: Walk In' for line in out))
  self.assertTrue(any('Walk-in sale; customer details are optional.' in line for line in out))
  self.assertFalse(any('Verified Shopify customer flags' in line for line in out))
 def test_ambiguous_customer_lookup_fails_closed_without_displaying_tags_or_registering(self):
  class AmbiguousCustomer(Api):
   def request(self,method,path,body=None):
    if 'customer-prescription' in path:
     self.calls.append((method,path,body))
     raise ApiError('More than one customer matches this phone number.',status=409,
                    payload={'status':'customer_ambiguous'})
    return super().request(method,path,body)
  api=AmbiguousCustomer();cli,out=self.make(['A B','9000000001'],api)
  with self.assertRaises(ApiError):
   cli._sale_customer(date(2026,10,27))
  self.assertFalse(any('Verified Shopify customer flags' in line for line in out))
  self.assertFalse(any(call[1]=='/api/customers' for call in api.calls if len(call)>1))
 def test_invoice_lookup_accepts_invoice_number_and_opens_print_page(self):
  class InvoiceApi(Api):
   def request(self,method,path,body=None):
    self.calls.append((method,path,body))
    if path.startswith('/api/invoices/lookup?'):
     return 200,{'sale_id':'sale_1','status':'COMPLETED','invoice_no':'ELU/MM/FY26/00001','order_id':'99'}
    return super().request(method,path,body)
  api=InvoiceApi();opened=[]
  output=[];cli=CashierCLI(api,lambda _:'ELU/MM/FY26/00001',output.append,browser_open=lambda url,new=0:opened.append(url) or True)
  cli.invoice()
  self.assertEqual('GET',api.calls[0][0])
  self.assertEqual('/api/invoices/lookup?invoice_no=ELU%2FMM%2FFY26%2F00001',api.calls[0][1])
  self.assertEqual(['http://127.0.0.1:8790/api/invoices/sale_1'],opened)
  self.assertTrue(any('Sale sale_1 status: COMPLETED' in line for line in output))
 def test_completed_checkout_opens_invoice_when_browser_is_configured(self):
  api=Api();opened=[];cli,out=self.make([],api)
  cli.browser_open=lambda url,new=0:opened.append(url) or True
  cli.draft={'idempotency_key':'stable-browser','business_date':'2026-10-27','channel':'walk_in','staff_id':'S1','cart':{'store_code':'HSR','location_id':'7','lines':[{'variant_id':'1','quantity':1}]}}
  answers=iter(['upi','y']);cli.input=lambda _:next(answers)
  cli.complete_checkout()
  self.assertEqual(['http://127.0.0.1:8790/api/invoices/sale_1'],opened)
 def test_open_till_list_explains_live_ids_instead_of_sample_ids(self):
  class TillApi(Api):
   def request(self,method,path,body=None):
    self.calls.append((method,path,body))
    if path=='/api/tills?store_code=MM':
     return 200,{'tills':[]}
    return super().request(method,path,body)
  api=TillApi();cli,out=self.make(['','MM'],api)
  cli.till(False)
  self.assertTrue(any('sample till IDs are not active sessions' in line for line in out))
 def test_malformed_checkout_response_is_reported_as_uncertain(self):
  class Response:
   status=201
   def __enter__(self):return self
   def __exit__(self,*_):pass
   def read(self):return b'not-json'
  with patch('eluno_pos.cli.urlopen',return_value=Response()):
   with self.assertRaises(ApiError) as raised:ApiClient().request('POST','/api/checkouts',{})
  self.assertTrue(raised.exception.uncertain)
 def test_customer_lookup_and_checkout_cancellation_preserves_draft(self):
  api=Api();cli,out=self.make(['+919000000001'],api);cli.find_customer();self.assertTrue(any('Customer: A B' in x for x in out))
  cli.draft={'idempotency_key':'stable','business_date':'2026-10-26','channel':'walk_in','staff_id':'S1','cart':{'store_code':'HSR','location_id':'7','lines':[{'variant_id':'1','quantity':1}]}}
  cli.input=lambda _: 'upi' if not hasattr(cli,'asked') else 'n'
  # Explicitly drive the confirmation through a deterministic small iterator.
  values=iter(['upi','n']);cli.input=lambda _:next(values);cli.complete_checkout()
  self.assertEqual('stable',cli.draft['idempotency_key']);self.assertFalse(any(c[1]=='/api/checkouts' for c in api.calls if len(c)>1))

 def test_existing_customer_without_valid_prescription_can_buy_a_frame(self):
  class NoValidPrescription(Api):
   def request(self,method,path,body=None):
    if 'customer-prescription' in path:
     self.calls.append((method,path,body))
     return 200,{'status':'no_valid_prescription','message':'No valid prescription is on file for this date.','customer':{'id':1,'first_name':'A','last_name':'B','phone':'+919000000001'}}
    return super().request(method,path,body)
  api=NoValidPrescription();output=[]
  pricing=SimpleNamespace(lines=(SimpleNamespace(catalog_variant=SimpleNamespace(line_type='frame'),cart_line=SimpleNamespace(quantity=1),payable_paise=10000),),gross_paise=10000,discount_paise=0,taxable_paise=8475,cgst_paise=763,sgst_paise=762,payable_paise=10000)
  answers=iter(['HSR','7','','','A B','9000000001','S1','1','1','1','','','done','1','y','upi','y'])
  cli=CashierCLI(api,lambda _:next(answers),output.append,preview=lambda _:pricing)
  cli.new_sale()
  payload=next(call[2] for call in api.calls if len(call)>2 and call[1]=='/api/checkouts')
  self.assertEqual('1',payload['customer_id'])
  self.assertEqual('+919000000001',payload['customer_phone'])
  self.assertTrue(any('frame-only sales may continue' in line for line in output))
  self.assertTrue(any('Customer: A B (1)' in line for line in output))
  self.assertTrue(any('No valid prescription is on file for this date.' in line for line in output))

 def test_lens_validation_errors_are_actionable_and_mismatched_variant_is_rejected(self):
  class LensValidation(Api):
   def __init__(self):super().__init__();self.validation_calls=0
   def request(self,method,path,body=None):
    if path=='/api/lens-validation':
     self.validation_calls+=1
     if self.validation_calls==1:
      raise ApiError('Lens configuration is invalid.',status=422,payload={'valid':False,'errors':['Right eye sphere is outside this lens range.']})
     return 200,{'valid':True,'errors':[],'variant_id':'91000001'}
    return super().request(method,path,body)
  api=LensValidation();cli,out=self.make([],api)
  cli.draft={'idempotency_key':'stable','business_date':'2026-10-26','channel':'walk_in','staff_id':'S1','cart':{'store_code':'HSR','location_id':'7','lines':[]}}
  rx={'rx_id':'RX1','phone':'+919000000001'}
  values=iter(['lens','91000002','1','','lens','91000002','1','','done'])
  cli.input=lambda _:next(values)
  cli._add_items(rx)
  self.assertEqual([],cli.draft['cart']['lines'])
  self.assertTrue(any('Right eye sphere is outside this lens range.' in line for line in out))
  self.assertTrue(any('selected product does not match' in line for line in out))

 def test_customer_and_item_choices_accept_numbered_menu_options(self):
  api=Api();cli,out=self.make(['Asha Rao','9876543210'],api)
  customer_id,phone,rx=cli._sale_customer(date.fromisoformat('2026-10-26'))
  self.assertEqual((42,'+919876543210',None),(customer_id,phone,rx))
  cli.draft={'idempotency_key':'stable','business_date':'2026-10-26','channel':'walk_in','staff_id':'S1','cart':{'store_code':'HSR','location_id':'7','lines':[]}}
  answers=iter(['1','1','1','','0'])
  cli.input=lambda _:next(answers)
  cli._add_items(None)
  self.assertEqual([{'variant_id':'1','quantity':1}],cli.draft['cart']['lines'])
  self.assertTrue(any('1. Frame' in line for line in out))

 def test_lens_selection_uses_catalog_configuration_and_requires_prescription(self):
  class ValidLensApi(Api):
   def request(self,method,path,body=None):
    if path=='/api/lens-validation':
     self.calls.append((method,path,body))
     return 200,{'valid':True,'errors':[],'variant_id':'91000002'}
    return super().request(method,path,body)
  api=ValidLensApi();cli,out=self.make([],api)
  cli.draft={'idempotency_key':'stable','business_date':'2026-10-26','channel':'walk_in','staff_id':'S1','cart':{'store_code':'HSR','location_id':'7','lines':[]}}
  answers=iter(['2','1','1','','0']);cli.input=lambda _:next(answers)
  cli._add_items({'rx_id':'RX1','phone':'+919000000001'})
  self.assertEqual('91000002',cli.draft['cart']['lines'][0]['variant_id'])
  validation=next(call[2] for call in api.calls if len(call)>2 and call[1]=='/api/lens-validation')
  self.assertEqual({'lens_type':'single_vision','index':'1.60','coating':'standard'},validation['lens'])
  cli.draft['cart']['lines']=[]
  answers=iter(['2','0']);cli.input=lambda _:next(answers)
  cli._add_items(None)
  self.assertEqual([],cli.draft['cart']['lines'])
  self.assertTrue(any('needs a valid prescription' in line for line in out))

 def test_manual_offer_choices_are_saved_for_preview_and_checkout(self):
  api=Api();cli,out=self.make(['2','1'],api)
  cli.draft={'idempotency_key':'stable','business_date':'2026-10-27','channel':'walk_in','staff_id':'S1','cart':{'store_code':'HSR','location_id':'7','lines':[{'variant_id':'1','quantity':1}]}}
  cli._select_offers(date.fromisoformat('2026-10-27'))
  self.assertEqual(['FESTIVE10'],cli.draft['offer_ids'])
  answers=iter(['upi','y']);cli.input=lambda _:next(answers)
  cli.complete_checkout()
  payload=next(call[2] for call in api.calls if len(call)>2 and call[1]=='/api/checkouts')
  self.assertEqual(['FESTIVE10'],payload['offer_ids'])
  self.assertTrue(any('Festive 10% on frames' in line for line in out))

 def test_channel_and_payment_selections_accept_numbered_options(self):
  cli,out=self.make(['2'])
  self.assertEqual('whatsapp_booked',cli._choose('Sales channel',(
   ('walk_in','Walk-in'),('whatsapp_booked','WhatsApp booked'),('online_pickup','Online pickup'),
  ),'1'))
  cli.draft={'idempotency_key':'stable','business_date':'2026-10-27','channel':'walk_in','staff_id':'S1','cart':{'store_code':'HSR','location_id':'7','lines':[{'variant_id':'1','quantity':1}]}}
  answers=iter(['2','y']);cli.input=lambda _:next(answers)
  cli.complete_checkout()
  payload=next(call[2] for call in cli.api.calls if len(call)>2 and call[1]=='/api/checkouts')
  self.assertEqual('card',payload['payment_method'])

 def test_yes_no_prompt_retries_invalid_words_instead_of_cancelling(self):
  cli,out=self.make(['maybe','y'])
  self.assertTrue(cli._yes('Submit sale'))
  self.assertTrue(any('Please enter Y or N.' in line for line in out))

 def test_add_prescription_cli_requires_confirmation_and_sends_idempotency_key(self):
  api=Api()
  answers=[
   '2026-10-27','Yashu Youwaraj','8123456789','JN','2027-10-27',
   '-1.00','-0.50','90','','-1.25','-0.25','85','',
   'S119','y',
  ]
  cli,out=self.make(answers,api)
  cli.add_prescription()
  payload=api.prescription_payload
  self.assertEqual('+918123456789',payload['phone'])
  registration=next(call[2] for call in api.calls if len(call)>2 and call[1]=='/api/customers')
  self.assertEqual('Yashu Youwaraj',registration['name'])
  self.assertEqual('S119',payload['optometrist_id'])
  self.assertEqual(85,payload['os']['axis'])
  self.assertTrue(payload['idempotency_key'].startswith('rx-'))
  self.assertTrue(any('Prescription RX-new saved' in line for line in out))

 def test_pairing_uses_numbered_choices_instead_of_manual_references(self):
  class ValidLensApi(Api):
   def request(self,method,path,body=None):
    if path=='/api/lens-validation':
     self.calls.append((method,path,body))
     return 200,{'valid':True,'errors':[],'variant_id':'91000002'}
    return super().request(method,path,body)
  api=ValidLensApi();cli,out=self.make([],api)
  cli.draft={'idempotency_key':'stable','business_date':'2026-10-26','channel':'walk_in','staff_id':'S1','cart':{'store_code':'HSR','location_id':'7','lines':[]}}
  answers=iter(['1','1','1','1','2','1','1','2','0'])
  cli.input=lambda _:next(answers)
  cli._add_items({'rx_id':'RX1','phone':'+919000000001'})
  lines=cli.draft['cart']['lines']
  self.assertEqual(2,len(lines))
  self.assertTrue(lines[0]['pair_ref'])
  self.assertEqual(lines[0]['pair_ref'],lines[1]['pair_ref'])
 def test_existing_customer_selection_keeps_customer_and_prescription(self):
  api=Api();cli,out=self.make(['A B','9000000001'],api)
  customer_id,phone,rx=cli._sale_customer(date.fromisoformat('2026-10-26'))
  self.assertEqual((1,'+919000000001','RX1'),(customer_id,phone,rx['rx_id']))
  self.assertTrue(any('Latest valid prescription: RX1' in line for line in out))
 def test_customer_is_found_automatically_by_mobile_and_saved_name_is_used(self):
  api=Api();cli,out=self.make(['Asha Rao','9000000001'],api)
  customer_id,phone,rx=cli._sale_customer(date.fromisoformat('2026-10-26'))
  self.assertEqual((1,'+919000000001','RX1'),(customer_id,phone,rx['rx_id']))
  self.assertFalse(any(call[1]=='/api/customers' for call in api.calls if len(call)>1))
  self.assertTrue(any('already registered to A B' in line for line in out))
 def test_checkout_reuses_draft_idempotency_key_and_reports_complete(self):
  api=Api();cli,out=self.make([],api);cli.draft={'idempotency_key':'stable','business_date':'2026-10-26','channel':'walk_in','staff_id':'S1','cart':{'store_code':'HSR','location_id':'7','lines':[{'variant_id':'1','quantity':1}]}}
  values=iter(['upi','y']);cli.input=lambda _:next(values);cli.complete_checkout()
  payload=[c[2] for c in api.calls if len(c)>2 and c[1]=='/api/checkouts'][0]
  self.assertEqual('stable',payload['idempotency_key']);self.assertIn('sale_1', ' '.join(out))
 def test_keyboard_interrupt_is_graceful(self):
  cli=CashierCLI(Api(),lambda _:(_ for _ in ()).throw(KeyboardInterrupt()),lambda _:None)
  self.assertEqual(130,cli.run())
 def test_six_section_menu_and_guided_frame_sale(self):
  api=Api(); output=[]
  pricing=SimpleNamespace(lines=(SimpleNamespace(catalog_variant=SimpleNamespace(line_type='frame'),cart_line=SimpleNamespace(quantity=1),payable_paise=10000),),gross_paise=10000,discount_paise=0,taxable_paise=8475,cgst_paise=763,sgst_paise=762,payable_paise=10000)
  answers=iter(['HSR','7','','','','','S1','1','1','1','','','done','1','y','upi','y'])
  cli=CashierCLI(api,lambda _:next(answers),output.append,preview=lambda _:pricing,browser_open=lambda *_args,**_kwargs:True)
  cli.new_sale()
  self.assertEqual(('Cashier','Admin & Management','System Status'),cli.MENU)
  payload=[call[2] for call in api.calls if len(call)>2 and call[1]=='/api/checkouts'][0]
  self.assertTrue(payload['idempotency_key'].startswith('cli-'))
  self.assertEqual('upi',payload['payment_method']);self.assertTrue(any('Invoice: INV1' in line for line in output))
 def test_new_customer_validation_keeps_name_and_checkout_uses_saved_identity(self):
  api=Api();output=[]
  pricing=SimpleNamespace(lines=(SimpleNamespace(catalog_variant=SimpleNamespace(line_type='frame'),cart_line=SimpleNamespace(quantity=1),payable_paise=10000),),gross_paise=10000,discount_paise=0,taxable_paise=8475,cgst_paise=763,sgst_paise=762,payable_paise=10000)
  answers=iter(['HSR','7','','','Asha Rao','123','9876543210','S1','1','2','2','','done','1','','upi','y'])
  cli=CashierCLI(api,lambda _:next(answers),output.append,preview=lambda _:pricing)
  cli.new_sale()
  registration=next(call[2] for call in api.calls if len(call)>2 and call[1]=='/api/customers')
  checkout=next(call[2] for call in api.calls if len(call)>2 and call[1]=='/api/checkouts')
  self.assertEqual({'name':'Asha Rao','phone':'+919876543210'},registration)
  self.assertEqual('42',checkout['customer_id'])
  self.assertEqual('+919876543210',checkout['customer_phone'])
  self.assertTrue(any('Please correct the mobile number' in message for message in output))

 def test_customer_registration_conflict_reports_duplicate_phone(self):
  class ConflictingApi(Api):
   def request(self,method,path,body=None):
    if path=='/api/customers' and not any(call[1]=='/api/customers' for call in self.calls if len(call)>1):
     self.calls.append((method,path,body));raise ApiError('phone belongs to another customer',status=409)
    return super().request(method,path,body)
  api=ConflictingApi();cli,out=self.make(['Asha Rao','9876543210'],api)
  with self.assertRaises(ApiError):
   cli._sale_customer(date.fromisoformat('2026-10-26'))
  registration=[call[2] for call in api.calls if len(call)>2 and call[1]=='/api/customers'][-1]
  self.assertEqual({'name':'Asha Rao','phone':'+919876543210'},registration)

 def test_cancelling_customer_conflict_stops_the_sale(self):
  class Conflict(Api):
   def request(self,method,path,body=None):
    if path=='/api/customers':
     self.calls.append((method,path,body));raise ApiError('phone belongs to another customer',status=409)
    return super().request(method,path,body)
  api=Conflict();cli,out=self.make(['HSR','7','','','Asha Rao','9876543210'],api)
  with self.assertRaises(ApiError):
   cli.new_sale()
  self.assertIsNone(cli.draft)
  self.assertFalse(any(call[1]=='/api/checkouts' for call in api.calls if len(call)>1))

 def test_walk_in_sale_does_not_register_or_attach_a_customer(self):
  api=Api();cli,out=self.make(['HSR','7','','','','','S1','1','1','1','','','done','1','','upi','y'],api)
  pricing=SimpleNamespace(lines=(SimpleNamespace(catalog_variant=SimpleNamespace(line_type='frame'),cart_line=SimpleNamespace(quantity=1),payable_paise=10000),),gross_paise=10000,discount_paise=0,taxable_paise=8475,cgst_paise=763,sgst_paise=762,payable_paise=10000)
  cli.preview=lambda _:pricing;cli.new_sale()
  checkout=next(call[2] for call in api.calls if len(call)>2 and call[1]=='/api/checkouts')
  self.assertIsNone(checkout['customer_id']);self.assertIsNone(checkout['customer_phone'])
  self.assertFalse(any(call[1]=='/api/customers' for call in api.calls if len(call)>1))
 def test_cash_sale_checks_open_matching_till_before_checkout(self):
  class NoOpenTill(Api):
   def request(self,method,path,body=None):
    if path=='/api/tills?store_code=HSR':return 200,{'tills':[]}
    return super().request(method,path,body)
  api=NoOpenTill();cli,out=self.make([],api);cli.draft={'idempotency_key':'stable','business_date':'2026-10-26','channel':'walk_in','staff_id':'S1','cart':{'store_code':'HSR','location_id':'7','lines':[{'variant_id':'1','quantity':1}]}}
  answers=iter(['1','n']);cli.input=lambda _:next(answers)
  with self.assertRaisesRegex(ValueError,'needs an open till'): cli.complete_checkout()
  self.assertFalse(any(call[1]=='/api/checkouts' for call in api.calls if len(call)>1))
 def test_cash_checkout_automatically_uses_the_open_till_for_the_sale_location(self):
  class OpenTillApi(Api):
   def request(self,method,path,body=None):
    if path=='/api/tills?store_code=HSR':
     self.calls.append((method,path,body))
     return 200,{'tills':[
      {'session_id':'other-location','location_id':'8','expected_paise':1000},
      {'session_id':'jn-open-till','location_id':'7','expected_paise':5000},
     ]}
    return super().request(method,path,body)
  api=OpenTillApi();cli,out=self.make(['1','y'],api)
  cli.draft={'idempotency_key':'stable','business_date':'2026-10-26','channel':'walk_in','staff_id':'S1','cart':{'store_code':'HSR','location_id':'7','lines':[{'variant_id':'1','quantity':1}]}}
  cli._open_invoice=lambda sale_id:None
  cli.complete_checkout()
  checkout=next(call[2] for call in api.calls if len(call)>2 and call[1]=='/api/checkouts')
  self.assertEqual('jn-open-till',checkout['till_session_id'])
  self.assertTrue(any('Using open till session jn-open-till' in line for line in out))
  self.assertFalse(any('Open till session ID' in line for line in out))
 def test_cash_checkout_can_open_a_till_inline_when_none_is_open(self):
  class TillApi(Api):
   def request(self,method,path,body=None):
    if path=='/api/tills?store_code=HSR':
     self.calls.append((method,path,body));return 200,{'tills':[]}
    if method=='POST' and path=='/api/tills':
     self.calls.append((method,path,body));self.open_payload=body
     return 201,{'session_id':'new-till','status':'OPEN'}
    return super().request(method,path,body)
  api=TillApi();cli,out=self.make(['1','y','','50.00','y'],api)
  cli.draft={'idempotency_key':'stable','business_date':'2026-10-26','channel':'walk_in','staff_id':'S1','cart':{'store_code':'HSR','location_id':'7','lines':[{'variant_id':'1','quantity':1}]}}
  cli._open_invoice=lambda sale_id:None
  cli.complete_checkout()
  checkout=next(call[2] for call in api.calls if len(call)>2 and call[1]=='/api/checkouts')
  self.assertEqual('new-till',checkout['till_session_id'])
  self.assertEqual({'store_code':'HSR','location_id':'7','operator':'S1','opening_float_paise':5000},api.open_payload)
 def test_unknown_checkout_keeps_draft_key_and_tells_cashier_not_to_resubmit(self):
  class Unknown(Api):
   def request(self,method,path,body=None):
    if path=='/api/checkouts': return 202,{'status':'ORDER_UNKNOWN','sale_id':'sale_uncertain','order_id':None,'invoice_no':None}
    return super().request(method,path,body)
  api=Unknown();cli,out=self.make([],api);cli.draft={'idempotency_key':'stable','business_date':'2026-10-26','channel':'walk_in','staff_id':'S1','cart':{'store_code':'HSR','location_id':'7','lines':[{'variant_id':'1','quantity':1}]}}
  answers=iter(['upi','y']);cli.input=lambda _:next(answers);cli.complete_checkout()
  self.assertEqual('stable',cli.draft['idempotency_key']);self.assertTrue(any('Do not submit another sale' in line for line in out))
  self.assertTrue(cli.draft['outcome_uncertain']);self.assertEqual('sale_uncertain',cli.draft['sale_id'])
 def test_uncertain_draft_blocks_new_sale_and_preserves_key(self):
  api=Api();cli,out=self.make([],api)
  cli.draft={'idempotency_key':'keep-this-key','sale_id':'sale_uncertain','outcome_uncertain':True}
  cli.new_sale()
  self.assertEqual('keep-this-key',cli.draft['idempotency_key'])
  self.assertFalse(any(call[1]=='/api/checkouts' for call in api.calls if len(call)>1))
  self.assertTrue(any('/api/checkouts/sale_uncertain/recover' in line for line in out))
 def test_lost_checkout_response_keeps_key_and_blocks_a_new_draft(self):
  class Lost(Api):
   def request(self,method,path,body=None):
    self.calls.append((method,path,body))
    if method=='POST' and path=='/api/checkouts':raise ApiError('connection lost',uncertain=True)
    return super().request(method,path,body)
  api=Lost();cli,out=self.make([],api)
  cli.draft={'idempotency_key':'stable-lost-key','business_date':'2026-10-26','channel':'walk_in','staff_id':'S1','cart':{'store_code':'HSR','location_id':'7','lines':[{'variant_id':'1','quantity':1}]}}
  answers=iter(['upi','y']);cli.input=lambda _:next(answers);cli.complete_checkout()
  self.assertEqual('stable-lost-key',cli.draft['idempotency_key'])
  self.assertTrue(cli.draft['outcome_uncertain'])
  calls=len(api.calls);cli.new_sale()
  self.assertEqual(calls,len(api.calls))
  self.assertTrue(any('operator reconciliation' in line for line in out))
 def test_top_level_cashier_admin_and_status_navigation_return(self):
  cli,out=self.make(['1','0','2','0','3','0','0'])
  self.assertEqual(0,cli.run())
  text='\n'.join(out)
  self.assertIn('CASHIER',text);self.assertIn('ADMIN & MANAGEMENT',text);self.assertIn('SYSTEM STATUS',text)
  self.assertIn('Shopify adapter',text);self.assertIn('REACHABLE',text)
 def test_reachable_terminal_keeps_overall_status_degraded(self):
  cli,out=self.make(['0'])
  cli._ledger_status=lambda:('HEALTHY','test ledger is readable')
  cli.system_status()
  self.assertTrue(any('Payment terminal' in line and 'REACHABLE' in line for line in out))
  self.assertTrue(any('Overall: DEGRADED' in line for line in out))
 def test_verified_local_mock_readiness_allows_ready_when_required_checks_pass(self):
  class ReadyLocal(Api):
   def request(self,method,path,body=None):
    if path=='/api/system-status':
     return 200,{'environment':'LOCAL_MOCK','components':[
      {'name':'Shopify adapter','status':'HEALTHY','explanation':'ok','required':True},
      {'name':'Store-tool adapter','status':'HEALTHY','explanation':'ok','required':True},
      {'name':'Payment terminal','status':'REACHABLE','explanation':'connectivity only','required':False},
      {'name':'Local mock payment flow','status':'HEALTHY','explanation':'fixed mock charge verified','required':True},
      {'name':'Real payment terminal','status':'NOT_CONFIGURED','explanation':'local mock only','required':False}]}
    return super().request(method,path,body)
  cli,out=self.make(['0'],ReadyLocal())
  cli._ledger_status=lambda:('HEALTHY','test ledger is readable')
  cli.system_status()
  self.assertTrue(any('Environment: LOCAL MOCK' in line for line in out))
  self.assertTrue(any('Overall: READY' in line for line in out))
 def test_local_mock_payment_test_requires_confirmation_and_reports_result(self):
  class LocalMock(Api):
   def __init__(self):super().__init__();self.tested=False
   def request(self,method,path,body=None):
    if method=='POST' and path=='/api/system-status/mock-payment-test':
     self.tested=True;self.calls.append((method,path,body))
     return 200,{'status':'HEALTHY','explanation':'fixed mock charge verified'}
    if path=='/api/system-status':
     self.calls.append((method,path,body))
     return 200,{'environment':'LOCAL_MOCK','components':[
      {'name':'Shopify adapter','status':'HEALTHY','explanation':'ok','required':True},
      {'name':'Store-tool adapter','status':'HEALTHY','explanation':'ok','required':True},
      {'name':'Payment terminal','status':'REACHABLE','explanation':'connectivity only','required':False},
      {'name':'Local mock payment flow','status':'UNKNOWN' if not self.tested else 'HEALTHY','explanation':'mock test state','required':True},
      {'name':'Real payment terminal','status':'NOT_CONFIGURED','explanation':'local mock only','required':False}]}
    return super().request(method,path,body)
  api=LocalMock();cli,out=self.make(['3','y','0'],api)
  cli._ledger_status=lambda:('HEALTHY','test ledger is readable')
  cli.system_status()
  self.assertTrue(api.tested)
  self.assertTrue(any('fixed mock charge verified' in line for line in out))
  self.assertTrue(any('Overall: READY' in line for line in out))
 def test_status_unavailable_does_not_report_ready_or_write(self):
  class Down(Api):
   def request(self,method,path,body=None):
    if path=='/health': raise ApiError('connection refused')
    return super().request(method,path,body)
  api=Down();cli,out=self.make(['0'],api);cli.system_status()
  self.assertTrue(any('Overall: UNAVAILABLE' in line for line in out))
  self.assertFalse(any(call[0]=='POST' for call in api.calls))
 def test_configuration_summary_redacts_credentials(self):
  api=Api();api.base_url='http://user:secret@127.0.0.1:8790/token'
  cli,out=self.make([],api);cli.configuration_summary()
  self.assertNotIn('secret','\n'.join(out));self.assertIn('127.0.0.1', '\n'.join(out))
  self.assertIn(':8790/token', '\n'.join(out))
