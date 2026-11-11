import { useEffect, useState, useRef } from 'react';
import { jsx } from 'react/jsx-runtime';
import { h, money, Approval, JobDetail, PaymentForm } from './components.js';
import { themeCSS, ACTIVE_THEME, flowCSS } from './tokens.js';

const STAGES = ['enquiry', 'quoted', 'accepted', 'scheduled', 'done', 'invoiced', 'paid'];
const LABELS = { enquiry:'Enquiry', quoted:'Quoted', accepted:'Accepted', scheduled:'Scheduled', done:'Done', invoiced:'Invoiced', paid:'Paid', declined:'Declined', cancelled:'Cancelled' };

function Pipeline({ overview, selected, open }) {
  function lane(state) {
    const cards = overview.pipeline[state] || [];
    return h('section', { className:'lane', key:state, 'data-state':state },
      h('div', {className:'lane-heading'}, h('h3', {}, LABELS[state]), h('span', {}, cards.length)),
      ...cards.map(card => h('article', {className:'job', key:card.id, 'data-job-number':card.number},
        h('button', {className:'job-select', 'aria-pressed':selected===card.number,
          'aria-controls':'job-detail', onClick:()=>open(card.number)}, card.customer_name),
        h('span', {className:'job-description'}, card.description),
        h('span', {className:'job-bottom'}, h('span', {className:'figure'}, card.total_minor == null ? 'Unpriced' : money(card.total_minor,card.currency,overview.locale)),
          h('span', {className:'job-id'}, card.number)))));
  }
  const total = Object.values(overview.pipeline).reduce((count,rows)=>count+rows.length,0);
  return h('section', {id:'pipeline', className:'pipeline', 'data-view':'pipeline'},
    h('div', {className:'region-heading'}, h('h2', {}, 'Job pipeline'), h('p', {className:'muted'}, total+' jobs')),
    total ? h('div', {className:'board',tabIndex:0,role:'region','aria-label':'Job pipeline, scroll sideways to view stages'}, ...STAGES.map(lane))
      : h('p', {className:'empty-state'}, 'No jobs yet. Capture an enquiry in your owner chat to begin.'),
    ['declined','cancelled'].some(state=>overview.pipeline[state]?.length) &&
      h('details', {className:'closed-jobs'}, h('summary', {}, 'Declined and cancelled jobs'),
        h('div', {className:'closed-board'}, ...['declined','cancelled'].map(lane))));
}

export function OfficePage({ ctx }) {
  const [overview,setOverview]=useState(null);
  const [job,setJob]=useState(null);
  const [selected,setSelected]=useState(null);
  const [error,setError]=useState('');
  const [jobError,setJobError]=useState('');
  const [loadingJob,setLoadingJob]=useState(false);
  const [busy,setBusy]=useState(false);
  const [status,setStatus]=useState('');
  const [from,setFrom]=useState('');
  const [to,setTo]=useState('');
  const selection=useRef(null);
  const request=useRef(0);
  async function open(number) {
    const version=++request.current;
    selection.current=number; setSelected(number); setLoadingJob(true); setJobError('');
    try {
      const data=await ctx.rest('/jobs/'+number);
      if(version===request.current)setJob(data);
    } catch(error) {if(version===request.current)setJobError(String(error));}
    finally {if(version===request.current)setLoadingJob(false);}
  }
  async function refresh() {
    const data=await ctx.rest('/overview');
    setOverview(data); setError('');
    const number=selection.current || data.pipeline.invoiced?.[0]?.number || Object.values(data.pipeline).flat()[0]?.number;
    if(number)await open(number);
  }
  useEffect(()=>{refresh().catch(error=>setError(String(error)));return()=>{request.current++;};},[ctx]);
  async function act(path,body={}) {
    setBusy(true);setError('');setStatus('');
    try {
      const result=await ctx.rest(path,{method:'POST',body});
      setStatus(result.code ? result.code+' / '+result.status+(result.delivery?' / delivery '+result.delivery:''):'Saved');
      await refresh(); return true;
    } catch(error) {setError(String(error));return false;}
    finally {setBusy(false);}
  }
  async function download(event) {
    event.preventDefault();setError('');setStatus('');setBusy(true);
    try {
      const data=await ctx.rest('/export',{method:'POST',body:{from,to}});
      const link=document.createElement('a');link.href='data:application/zip;base64,'+data.base64;
      link.download=data.filename;link.click();setStatus('Export downloaded / '+data.filename);
    } catch(error) {setError(String(error));}
    finally {setBusy(false);}
  }
  const feedback=error && h('div',{role:'alert',className:'error-state'}, h('p',{},error),
    h('button',{onClick:()=>{setError('');refresh().catch(error=>setError(String(error)));}},'Try again'));
  return h('div',{className:'chalkline-ui','data-theme':ACTIVE_THEME},
    h('style',{},themeCSS()+flowCSS),
    h('a',{className:'skip',href:'#pipeline'},'Skip to jobs'),
    h('div',{className:'app'},h('header',{className:'topbar'},
      h('a',{className:'wordmark',href:'#top'},'Chalkline'),
      h('nav',{'aria-label':'Office navigation'},h('a',{href:'#pipeline','aria-current':'page'},'Jobs'),
        h('a',{href:'#approvals'},'Approvals ',overview && h('span',{},overview.approvals.length)),
        h('a',{href:'#money'},'Money')),
      h('span',{className:'owner'},overview?.business_name)),
    h('main',{id:'top','data-page':'chalkline'},
      h('header',{className:'page-heading'},h('div',{},h('h1',{},'The work, in order.'),h('p',{},'Voice note in. Paperwork out.')),
        h('div',{className:'page-tools'},overview&&h('time',{dateTime:overview.today},overview.today),
          h('button',{className:'secondary',disabled:busy,onClick:()=>refresh().catch(error=>setError(String(error)))},'Refresh'))),
      feedback,status&&h('p',{role:'status',className:'feedback'},status),
      !overview ? h('p',{className:'loading-state',role:'status'},error?'Office unavailable. Retry when your backend is connected.':'Loading your office…') :
      h('fieldset',{disabled:busy,className:'office-content','aria-busy':busy},
        h('dl',{id:'money',className:'money','data-view':'money'},
          ...[['owed_minor','Owed'],['overdue_minor','Overdue'],['paid_this_month_minor','Paid this month'],['expenses_this_month_minor','Expenses this month']].map(([key,label])=>
            h('div',{key},h('dt',{},label),h('dd',{className:'figure'},money(overview.money[key],overview.currency,overview.locale))))),
        h('div',{className:'workbench'},h('div',{className:'work'},
          jsx(Pipeline,{overview,selected,open}),
          h('section',{id:'approvals',className:'approvals','data-view':'approvals'},
            h('div',{className:'region-heading'},h('h2',{},'Pending approvals'),h('p',{className:'muted'},overview.approvals.length+' to review')),
            overview.approvals.length ? overview.approvals.map(row=>jsx(Approval,{row,act},row.code)):
              h('p',{className:'empty-state'},'No approvals waiting. New proposals will appear here.')),
          h('section',{className:'operations','data-view':'appointments'},h('h2',{},'This week'),
            overview.appointments.length ? h('ul',{className:'record-list'},...overview.appointments.map(row=>
              h('li',{key:row.id},h('span',{},h('button',{className:'text-action',onClick:()=>open(row.job_number)},row.job_number+' / '+row.customer_name),
                h('small',{},new Date(row.start).toLocaleString(overview.locale,{timeZone:overview.timezone}))))))
              : h('p',{className:'empty-state'},'No appointments scheduled this week.')),
          h('section',{className:'operations','data-view':'invoices'},h('h2',{},'Outstanding invoices'),
            overview.invoices.length ? overview.invoices.map(invoice=>h('details',{key:invoice.id,className:'payment-record'},
              h('summary',{},invoice.number+' / '+invoice.customer_name+' / '+money(invoice.balance_minor,invoice.currency)),
              h('p',{className:'muted'},'Due '+invoice.due_at),jsx(PaymentForm,{invoice,act})))
              : h('p',{className:'empty-state'},'No outstanding invoices.')),
          h('section',{className:'operations','data-view':'export'},h('h2',{},'Accountant export'),
            h('form',{className:'export-form',onSubmit:download},
              h('label',{},'From',h('input',{type:'date',required:true,value:from,onChange:event=>setFrom(event.target.value)})),
              h('label',{},'To',h('input',{type:'date',required:true,value:to,min:from,onChange:event=>setTo(event.target.value)})),
              h('button',{className:'primary',type:'submit'},'Download ZIP')))),
          h('aside',{id:'job-detail',className:'inspector','aria-label':'Selected job', 'aria-busy':loadingJob},
            jobError ? h('div',{role:'alert'},h('p',{},jobError),h('button',{onClick:()=>open(selected)},'Retry job')) :
              loadingJob ? h('p',{role:'status'},'Loading job…') :
                job ? jsx(JobDetail,{data:job,ctx,act,locale:overview.locale,timezone:overview.timezone}) : h('p',{className:'empty-state'},'Select a job to see its evidence and paperwork.')))),
      h('footer',{},overview?.business_name || 'Chalkline'))));
}
