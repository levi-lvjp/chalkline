import { jsx } from 'react/jsx-runtime';
import { useEffect, useState } from 'react';
import { money } from './money.js';
export { money } from './money.js';

export function h(type, props, ...children) {
  if (children.length) return jsx(type, { ...props, children });
  return jsx(type, props);
}

export function Evidence({ source, ctx }) {
  const [image, setImage] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    if (source.thumbnail_url) {
      ctx.rest(source.thumbnail_url).then(data => { if (active) setImage(data.data_url); })
        .catch(() => { if (active) setError('Thumbnail unavailable'); });
    }
    return () => { active = false; };
  }, [source.id, ctx]);
  return h('li', { 'data-source-id': source.id, className: 'evidence' },
    h('strong', {}, source.kind), h('blockquote', {}, source.text),
    image && h('img', { src: image, alt: 'Job evidence', width: 320, style: { maxWidth: '100%', height: 'auto' } }),
    error && h('p', { role: 'status' }, error));
}

export function Approval({ row, act }) {
  return h('details', {className:'approval','data-approval-code':row.code},
    h('summary',{},h('span',{className:'approval-code'},row.code),
      h('span',{className:'approval-title'},row.summary,h('small',{},row.recipient||'Owner record')),
      row.amount_minor!=null && h('span',{className:'figure'},money(row.amount_minor,row.currency))),
    h('div',{className:'approval-content'},
      h('div',{className:'message'},h('p',{},row.body||''),
        row.attachment_name && h('span',{},row.attachment_name),
        Object.keys(row.details||{}).length>0 && h('details',{},h('summary',{},'Proposal details'),h('pre',{},JSON.stringify(row.details,null,2))),
        h('span',{},'Expires '+new Date(row.expires_at).toLocaleString())),
      h('div',{className:'approval-actions'},
        h('button',{className:'primary',onClick:()=>act('/approvals/'+row.code+'/approve')},'Approve'),
        h('button',{onClick:()=>act('/approvals/'+row.code+'/reject')},'Reject'))));
}

export function PaymentForm({ invoice, act }) {
  const [amount, setAmount] = useState('');
  const [reference, setReference] = useState('');
  return h('form', { className: 'payment-form', onSubmit: event => {
    event.preventDefault();
    act('/invoices/' + invoice.number + '/payments', { amount, reference });
  } },
  h('label', {}, 'Payment amount', h('input', { required: true, inputMode: 'decimal', value: amount,
    onChange: event => setAmount(event.target.value) })),
  h('label', {}, 'Reference', h('input', { value: reference, onChange: event => setReference(event.target.value) })),
  h('p', {className:'muted'}, 'Creates a proposal for your approval.'), h('button', { className:'primary', type: 'submit' }, 'Propose payment'));
}

export function JobDetail({ data, ctx, act, locale, timezone }) {
  const currency=data.invoices[0]?.currency || data.quotes[0]?.currency;
  const empty=text=>h('p',{className:'muted'},text);
  return h('section', { 'data-view': 'job-detail' },
    h('header',{className:'detail-head'},h('span',{className:'job-id'},data.job.number),h('span',{className:'status'},data.job.state)),
    h('h2',{},data.job.description),h('p',{},data.customer.name),
    h('p',{className:'muted'},data.customer.phone || 'Phone unavailable'),
    h('p',{className:'muted'},data.job.site_address || 'Address not recorded'),
    h('h3',{},'Sources'),data.sources.length ? h('ul',{className:'record-list'},...data.sources.map(source=>jsx(Evidence,{source,ctx},source.id))) : empty('No sources captured.'),
    h('h3',{},'Quote revisions'),data.quotes.length ? data.quotes.map(quote=>h('article',{key:quote.id,className:'quote-record'},
      h('h4',{},quote.number+' revision '+quote.revision),h('small',{},quote.status==='sent'?'Issued':quote.status),
      h('p',{className:'figure'},money(quote.total_minor,quote.currency)),h('ul',{className:'record-list'},...quote.lines.map((line,index)=>h('li',{key:index},
        h('span',{},line.description,h('small',{},'Quantity '+line.qty)),h('span',{className:'figure'},money(line.total_minor,quote.currency))))))) : empty('No quote yet.'),
    h('h3',{},'Extras'),data.variations.length ? ['proposed','agreed','declined'].map(status=>{
      const rows=data.variations.filter(extra=>extra.status===status);
      return rows.length>0 && h('section',{key:status},h('h4',{},status),h('ul',{className:'record-list'},...rows.map(extra=>h('li',{key:extra.id},
        h('span',{},extra.description),h('span',{className:'figure'},money(extra.amount_minor,currency))))));
    }) : empty('No extras recorded.'),
    h('h3',{},'Invoices'),data.invoices.length ? data.invoices.map(invoice=>h('article',{key:invoice.id,className:'invoice-record'},
      h('h4',{},invoice.number),h('small',{},invoice.status==='sent'?'Issued / delivery '+(invoice.delivery_status==='not_sent'?'queued':invoice.delivery_status):invoice.status),
      h('p',{className:'figure'},money(invoice.total_minor,invoice.currency)),h('ul',{className:'record-list'},...invoice.lines.map((line,index)=>h('li',{key:index},
        h('span',{},line.description),h('span',{className:'figure'},money(line.total_minor,invoice.currency))))),
      ['sent','on_hold'].includes(invoice.status) && jsx(PaymentForm,{invoice,act}))) : empty('No invoice yet.'),
    h('h3',{},'Payments'),data.payments.length ? h('ul',{className:'record-list'},...data.payments.map(payment=>h('li',{key:payment.id},
      h('span',{},payment.date,h('small',{},payment.method+' '+payment.reference)),h('span',{className:'figure'},money(payment.amount_minor,currency))))) : empty('No payments recorded.'),
    h('h3',{},'Audit timeline'),h('ol',{className:'timeline'},...data.timeline.map(event=>h('li',{key:event.id},
      h('time',{dateTime:event.created_at,title:event.created_at},new Date(event.created_at).toLocaleDateString(locale,{month:'short',day:'numeric',timeZone:timezone})),
      h('span',{},event.kind.replaceAll('_',' '),h('small',{className:'muted'},event.actor))))));
}
