import { useEffect, useMemo, useRef, useState } from 'react';
import type { ColumnDef } from '@tanstack/react-table';
import type { DataGridFeatures } from '@/components/reui/data-grid/data-grid';
import { Frame, FrameHeader, FrameTitle, FrameDescription, FramePanel } from '@/components/reui/frame';
import { Badge } from '@/components/reui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Checkbox } from '@/components/ui/checkbox';
import { Textarea } from '@/components/ui/textarea';
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@/components/ui/dialog';
import { AlertDialog, AlertDialogContent, AlertDialogHeader, AlertDialogTitle, AlertDialogDescription, AlertDialogFooter, AlertDialogAction, AlertDialogCancel } from '@/components/ui/alert-dialog';
import { Skeleton } from '@/components/ui/skeleton';
import { toast } from 'sonner';
import { RefreshCw, Download, Copy, Plus, Trash2, FolderOpen } from 'lucide-react';
import { Records } from './Records';
import { api, useSnapshot } from './api';

type Values=Record<string,unknown>;
type Field={key:string;label:string;kind:string;options:{value:string;label:string}[];readonly?:boolean;when?:Values;columns?:[string,string][]};
type Action={action:string;title:string;fields:Field[];values:Values;confirm?:string;module?:string;immediate?:boolean};
type Row=Values & {id?:string;sku?:string;_forms?:Action[];_details?:{label:string;value:unknown}[]};
type Table={title:string;columns:{key:string;label:string}[];rows:Row[]};
type Section={id:string;title:string;description?:string;text?:string;forms:Action[];tables:Table[];links?:{label:string;url:string}[]};
type View={module:string;filters:Field[];query:Values;sections:Section[]};
type Result={message?:string;document?:string;filename?:string;download?:string;forms?:Action[];tables?:Table[];select?:string};
type Job={id?:string;state:string;message?:string;result?:Result};
type Bridge={postMessage:(v:unknown)=>void;addEventListener:(name:string,callback:(event:MessageEvent)=>void)=>void;removeEventListener:(name:string,callback:(event:MessageEvent)=>void)=>void};
function bridge(){return (window as unknown as {chrome?:{webview?:Bridge}}).chrome?.webview;}
function text(value:unknown):string {if(value===null||value===undefined||value==='')return '—';if(typeof value==='boolean')return value?'是':'否';if(Array.isArray(value))return value.map(text).join('、');if(typeof value==='object')return Object.entries(value as Values).map(([k,v])=>`${k}：${text(v)}`).join('；');return String(value);}
const labels:Record<string,string>={sent:'已发送',pending:'待发送',unknown:'待核实',failed:'失败',partial:'部分送达',skipped:'已跳过',running:'处理中',paused:'已暂停',enabled:'启用',preview:'仅记录',cancelled:'已取消',webhook:'Webhook',enterprise:'企业机器人',inventory:'库存提醒',douyin_inventory:'抖店补库存',daily:'日报',messages:'货源转发',profit:'价格提醒'};
function display(value:unknown):string{if(Array.isArray(value))return value.map(display).join('、');return labels[String(value)]||text(value);}
function saveBlob(blob:Blob,name:string){const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function external(url:string){if(!url.startsWith('https://'))return;const host=bridge();if(host)host.postMessage({type:'open-external',url});else window.open(url,'_blank','noopener,noreferrer');}

/** 文件路径由用户选择，网页不会读取或上传浏览器登录资料。 */
function FileInput({value,onChange,label}:{value:string;onChange:(v:string)=>void;label:string}){
  const choose=()=>{const host=bridge();if(!host){toast.info('浏览器验收模式请填写本机文件完整路径');return;}
    const id=crypto.randomUUID();
    const listener=(event:MessageEvent)=>{if(event.data?.type==='chosen-file'&&event.data.id===id){host.removeEventListener('message',listener);if(event.data.path)onChange(event.data.path);}};
    host.addEventListener('message',listener);host.postMessage({type:'choose-file',id});};
  return <div className="flex min-w-0 gap-2"><Input aria-label={label} value={value} onChange={e=>onChange(e.target.value)}/><Button type="button" variant="outline" size="sm" onClick={choose}><FolderOpen/>选择文件</Button></div>;
}
function FieldInput({field,value,onChange}:{field:Field;value:unknown;onChange:(v:unknown)=>void}){
  const [search,setSearch]=useState('');
  const id='field-'+field.key;
  const choices=field.options||[];
  if(field.kind==='bool')return <label className="flex items-center gap-2"><Checkbox checked={!!value} onCheckedChange={onChange}/><span className="text-sm">{field.label}</span></label>;
  if(field.kind==='multi'){
    const values=Array.isArray(value)?value.map(String):[];
    return <fieldset className="flex min-w-0 flex-col gap-3"><legend className="text-sm font-medium">{field.label}</legend>{choices.length>8&&<Input aria-label={'筛选'+field.label} placeholder="搜索可选项" value={search} onChange={e=>setSearch(e.target.value)}/>}<div className="flex max-h-64 flex-col gap-3 overflow-y-auto p-1">{choices.filter(o=>o.label.includes(search)).map(o=><label key={o.value} className="flex items-center gap-2"><Checkbox checked={values.includes(o.value)} onCheckedChange={checked=>onChange(checked?[...values,o.value]:values.filter(v=>v!==o.value))}/><span className="break-all text-sm">{o.label}</span></label>)}{!choices.length&&<span className="text-sm text-muted-foreground">暂无可选项，请先添加配置。</span>}</div></fieldset>;
  }
  if(field.kind==='rows')return <RowEditor field={field} value={value} onChange={onChange}/>;
  const input=field.kind==='select'?<Select value={String(value??choices[0]?.value??'')} onValueChange={v=>onChange(v??'')} items={Object.fromEntries(choices.map(o=>[o.value,o.label]))}><SelectTrigger aria-label={field.label}><SelectValue/></SelectTrigger><SelectContent>{choices.map(o=><SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}</SelectContent></Select>:
    field.kind==='textarea'?<Textarea id={id} aria-label={field.label} className="min-h-32 max-h-96" value={String(value??'')} onChange={e=>onChange(e.target.value)}/>:
    field.kind==='file'?<FileInput label={field.label} value={String(value??'')} onChange={onChange}/>:
    <Input id={id} aria-label={field.label} readOnly={field.readonly} type={['password','number','date'].includes(field.kind)?field.kind:'text'} autoComplete={field.kind==='password'?'new-password':'off'} value={String(value??'')} onChange={e=>onChange(e.target.value)}/>;
  return <div className="flex min-w-0 flex-col gap-2"><label htmlFor={id} className="text-sm font-medium">{field.label}</label>{input}</div>;
}
function RowEditor({field,value,onChange}:{field:Field;value:unknown;onChange:(v:unknown)=>void}){
  const rows=(Array.isArray(value)?value:[]) as Values[];
  return <fieldset className="flex min-w-0 flex-col gap-3"><legend className="text-sm font-medium">{field.label}</legend>{rows.map((row,index)=><div key={index} className="grid min-w-0 grid-cols-1 gap-3 border-b pb-4 sm:grid-cols-2">{field.columns?.map(([key,label])=><FieldInput key={key} field={{key:field.key+index+key,label,kind:['AllowReopen','muted','excluded'].includes(key)?'bool':'text',options:[]}} value={row[key]} onChange={v=>onChange(rows.map((r,i)=>i===index?{...r,[key]:v}:r))}/>)}<Button type="button" size="sm" variant="ghost" onClick={()=>onChange(rows.filter((_,i)=>i!==index))}><Trash2/>移除此行</Button></div>)}<Button type="button" size="sm" variant="outline" onClick={()=>onChange([...rows,{}])}><Plus/>添加一行</Button></fieldset>;
}
function BusinessTable({table,onAction}:{table:Table;onAction:(a:Action)=>void}){
  const [query,setQuery]=useState('');const [detail,setDetail]=useState<Row|null>(null);
  const rows=useMemo(()=>table.rows.map((r,i)=>({...r,id:r.id||r.sku||String(i)} as Row)).filter(r=>!query||table.columns.some(c=>text(r[c.key]).toLowerCase().includes(query.toLowerCase()))),[table,query]);
  const columns=useMemo<ColumnDef<DataGridFeatures,Row>[]>(()=>[...table.columns.map(c=>({accessorKey:c.key,header:c.label,cell:({row}:{row:{original:Row}})=>['enabled','status','delivery_status','erp_state','activity','state'].includes(c.key)?<Badge variant={['failed','缺货','涨价','待核对'].includes(String(row.original[c.key]))?'warning-light':String(row.original[c.key])==='降价'?'success-light':'secondary'}>{display(row.original[c.key])}</Badge>:<span className="break-words">{display(row.original[c.key])}</span>})),{id:'actions',header:'操作',cell:({row})=><div className="flex flex-wrap gap-2">{(row.original._forms||[]).map((a,i)=><Button type="button" key={i} variant="ghost" size="sm" onClick={()=>onAction(a)}>{a.title}</Button>)}{!!(row.original._details||row.original.url)&&<Button type="button" size="sm" variant="ghost" onClick={()=>setDetail(row.original)}>详情</Button>}</div>}],[table.columns,onAction]);
  return <div className="flex min-w-0 flex-col gap-4"><div className="flex flex-wrap items-center justify-between gap-3"><h3 className="font-medium">{table.title}</h3><Input aria-label={'筛选'+table.title} placeholder="搜索当前记录" value={query} onChange={e=>setQuery(e.target.value)}/></div><Records rows={rows} columns={columns} loading={false}/>{!rows.length&&<Button type="button" variant="outline" size="sm" onClick={()=>setQuery('')}>清空列表筛选</Button>}<Dialog open={!!detail} onOpenChange={open=>{if(!open)setDetail(null);}}><DialogContent className="max-h-[85dvh] overflow-y-auto"><DialogHeader><DialogTitle>记录详情</DialogTitle><DialogDescription>完整编号和业务明细以当前记录为准。</DialogDescription></DialogHeader>{detail?._details?.map((entry,i)=><div key={i} className="flex flex-col gap-1"><span className="text-xs text-muted-foreground">{entry.label}</span><p className="whitespace-pre-wrap break-all text-sm">{text(entry.value)}</p></div>)}{detail?.url?<div className="flex flex-wrap gap-2"><Button type="button" variant="outline" size="sm" onClick={()=>external(String(detail.url))}>打开商品链接</Button><Button type="button" variant="outline" size="sm" onClick={()=>void navigator.clipboard.writeText(String(detail.url)).then(()=>toast.success('已复制'))}>复制链接</Button></div>:null}</DialogContent></Dialog></div>;
}
function ResultPanel({result,onAction}:{result:Result;onAction:(a:Action)=>void}){
  const download=async()=>{const response=await fetch('/api/business/download?key='+encodeURIComponent(result.download||''),{headers:{'X-Console-Token':location.hash.slice(1)}});if(!response.ok)throw Error('文件下载失败');saveBlob(await response.blob(),result.filename||'下载文件');};
  return <Frame spacing="sm"><FrameHeader><FrameTitle>操作结果</FrameTitle><FrameDescription>{result.message||'请核对以下内容再继续。'}</FrameDescription></FrameHeader>{(result.document!==undefined||result.download||!!result.tables?.length||!!result.forms?.length)&&<FramePanel><div className="flex min-w-0 flex-col gap-6">{result.document!==undefined&&<div className="flex flex-col gap-3"><Textarea aria-label="结果正文" readOnly className="min-h-48 max-h-96" value={result.document}/><div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="outline" onClick={()=>void navigator.clipboard.writeText(result.document||'').then(()=>toast.success('已复制'))}><Copy/>复制正文</Button><Button type="button" size="sm" variant="outline" onClick={()=>saveBlob(new Blob([result.document||''],{type:'text/markdown;charset=utf-8'}),result.filename||'业务记录.md')}><Download/>下载文件</Button></div></div>}{result.download&&<Button type="button" size="sm" variant="outline" onClick={()=>void download().catch(e=>toast.error(e.message))}><Download/>下载{result.filename}</Button>}{result.tables?.map((t,i)=><BusinessTable key={i} table={t} onAction={onAction}/>)}<div className="flex flex-wrap gap-2">{result.forms?.map((a,i)=><Button type="button" key={i} size="sm" variant="outline" onClick={()=>onAction(a)}>{a.title}</Button>)}</div></div></FramePanel>}</Frame>;
}

/** 七个业务页均由 React 控件组成；数据轮询不重置正在编辑的表单。 */
function initialValues(action:Action){const initial={...action.values};for(const f of action.fields||[]){if(initial[f.key]===undefined)initial[f.key]=f.kind==='select'?f.options[0]?.value||'':f.kind==='bool'?false:f.kind==='multi'||f.kind==='rows'?[]:'';}return initial;}
export function BusinessPage({module,openAddProduct=false}:{module:string;openAddProduct?:boolean}){
  const [query,setQuery]=useState<Values>({});const [draft,setDraft]=useState<Values>({});const [section,setSection]=useState('');
  const params=new URLSearchParams(Object.entries(query).map(([k,v])=>[k,String(v)]));
  const snapshot=useSnapshot<View>(`/api/business/view/${module}?${params}`);
  const [form,setForm]=useState<Action|null>(null);const [values,setValues]=useState<Values>({});const [confirmation,setConfirmation]=useState(false);
  const [submitting,setSubmitting]=useState(false);const [jobId,setJobId]=useState(()=>sessionStorage.getItem('business-job-'+module)||'');
  const job=useSnapshot<Job>(jobId?'/api/business/job?id='+encodeURIComponent(jobId):'/api/business/idle',1000);
  const busy=submitting||!!jobId&&!job.error&&(job.data?.id!==jobId||job.data?.state==='running');
  const [result,setResult]=useState<Result>();
  useEffect(()=>{if(job.data?.id!==jobId)return;if(job.data?.state==='done'){setResult(job.data.result);if(job.data.result?.select){setSection(job.data.result.select);setQuery({});setDraft({});}snapshot.refresh();}else if(job.data?.state==='failed')toast.error(job.data.message||'操作失败');},[job.data,jobId,snapshot.refresh]);
  const openAction=(action:Action)=>{if(action.immediate){void submit(action,action.values);return;}setValues(initialValues(action));setForm(action);};
  const submit=async(selected:Action|null=form,payload:Values=values)=>{if(!selected||busy)return;setSubmitting(true);setConfirmation(false);
    try{const response=await api<{job:string}>('/api/business/action',{module:selected.module||module,action:selected.action,values:payload,requestId:crypto.randomUUID()});setJobId(response.job);sessionStorage.setItem('business-job-'+module,response.job);setForm(null);setValues({});setResult(undefined);}
    catch(e){toast.error(e instanceof Error?e.message:'操作未提交');}finally{setSubmitting(false);}};
  const addOpened=useRef(false);
  useEffect(()=>{
    // 等管理页拿到原表单定义后打开一次，轮询不会打断编辑。
    if(module!=='monitor'||!openAddProduct||addOpened.current||busy)return;
    const add=snapshot.data?.sections.find(s=>s.id==='products')?.forms.find(a=>a.action==='save');
    if(!add)return;
    addOpened.current=true;setSection('products');setValues(initialValues(add));setForm(add);
  },[module,openAddProduct,snapshot.data,busy]);
  const active=snapshot.data?.sections.find(s=>s.id===section)||snapshot.data?.sections[0];
  return <div data-business-module={module} className="flex min-w-0 flex-col gap-6">
    <div className="flex flex-wrap items-center justify-between gap-3">{snapshot.data&&<Select value={active?.id||''} onValueChange={v=>setSection(v||'')} items={Object.fromEntries(snapshot.data.sections.map(s=>[s.id,s.title]))}><SelectTrigger aria-label="业务分区"><SelectValue/></SelectTrigger><SelectContent>{snapshot.data.sections.map(s=><SelectItem key={s.id} value={s.id}>{s.title}</SelectItem>)}</SelectContent></Select>}<Button type="button" size="sm" variant="outline" onClick={snapshot.refresh}><RefreshCw/>刷新页面</Button></div>
    {snapshot.error&&<div role="alert" className="flex items-center gap-3"><Badge variant="destructive-light">读取失败</Badge><span>{snapshot.error}</span><Button type="button" size="sm" variant="outline" onClick={snapshot.refresh}>重试</Button></div>}
    {snapshot.loading&&!snapshot.data&&<Skeleton className="h-64 w-full"/>}
    {!!snapshot.data?.filters.length&&<Frame spacing="sm"><FramePanel><form className="flex flex-wrap items-end gap-4" onSubmit={e=>{e.preventDefault();setQuery({...snapshot.data?.query,...draft});}}>{snapshot.data.filters.map(f=><FieldInput key={f.key} field={f} value={draft[f.key]??snapshot.data?.query[f.key]} onChange={v=>setDraft(old=>({...old,[f.key]:v}))}/>)}<Button type="submit" size="sm">应用筛选</Button></form></FramePanel></Frame>}
    {busy&&<p role="status" className="text-sm text-muted-foreground">正在处理业务操作，请等待实际结果。</p>}
    {job.error&&<div role="alert" className="flex flex-wrap items-center gap-3"><span>{job.error}，请先刷新业务状态核对上次结果。</span><Button type="button" variant="outline" size="sm" onClick={()=>{setJobId('');sessionStorage.removeItem('business-job-'+module);snapshot.refresh();}}>刷新并关闭任务提示</Button></div>}
    {job.data?.state==='failed'&&<p role="alert" className="text-sm">{job.data.message}</p>}
    {result&&<ResultPanel result={result} onAction={openAction}/>}
    {active&&<Frame spacing="sm"><FrameHeader><FrameTitle>{active.title}</FrameTitle><FrameDescription>{active.description||'查看当前状态，按需执行以下操作。'}</FrameDescription></FrameHeader><FramePanel><div className="flex min-w-0 flex-col gap-6">{active.text&&<p className="whitespace-pre-wrap text-sm text-muted-foreground">{active.text}</p>}<div className="flex flex-wrap gap-2">{active.forms.map((a,i)=><Button type="button" key={i} size="sm" variant={i===0?'default':'outline'} disabled={busy} onClick={()=>openAction(a)}>{a.title}</Button>)}{active.links?.map(l=><Button type="button" key={l.url} size="sm" variant="outline" onClick={()=>external(l.url)}>{l.label}</Button>)}</div>{active.tables.map((t,i)=><BusinessTable key={active.id+i} table={t} onAction={openAction}/>)}</div></FramePanel></Frame>}
    <Dialog open={!!form} onOpenChange={open=>{if(!open&&!submitting){setForm(null);setValues({});}}}><DialogContent className="max-h-[85dvh] overflow-y-auto"><DialogHeader><DialogTitle>{form?.title}</DialogTitle><DialogDescription>修改后提交生效，未提交的内容不会保存。</DialogDescription></DialogHeader><form className="flex min-w-0 flex-col gap-4" onSubmit={e=>{e.preventDefault();if(form?.confirm)setConfirmation(true);else void submit();}}>{form?.fields.filter(f=>!f.when||Object.entries(f.when).every(([k,v])=>values[k]===v)).map(f=><FieldInput key={f.key} field={f} value={values[f.key]} onChange={v=>setValues(old=>({...old,[f.key]:v}))}/>)}{module==='monitor'&&form?.action==='save'&&<Button type="button" size="sm" variant="outline" onClick={()=>setValues(old=>({...old,threshold:({'RTX 5070 及以上显卡':5,'其余显卡':20,'主板':10,'CPU':20,'内存':10} as Record<string,number>)[String(old.category)]??old.threshold}))}>使用分类建议阈值</Button>}<div className="flex justify-end gap-2"><Button type="button" size="sm" variant="outline" onClick={()=>setForm(null)}>取消</Button><Button type="submit" size="sm" disabled={busy}>提交</Button></div></form></DialogContent></Dialog>
    <AlertDialog open={confirmation} onOpenChange={setConfirmation}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{form?.title}</AlertDialogTitle><AlertDialogDescription>{form?.confirm}</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>取消</AlertDialogCancel><AlertDialogAction disabled={busy} onClick={()=>void submit()}>确认执行</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
  </div>;
}
