import { useEffect, useMemo, useRef, useState } from 'react';
import type { ColumnDef } from '@tanstack/react-table';
import type { DataGridFeatures } from '@/components/reui/data-grid/data-grid';
import { PackageSearch, ListChecks, Download, Settings, RefreshCw, Play, Pause, Moon, Sun, Boxes, Bell, ChartNoAxesCombined, Plus } from 'lucide-react';
import { Sidebar, SidebarProvider, SidebarHeader, SidebarContent, SidebarGroup, SidebarGroupLabel, SidebarMenu, SidebarMenuItem, SidebarMenuButton, SidebarFooter, SidebarInset, SidebarTrigger, useSidebar } from '@/components/ui/sidebar';
import { Frame, FramePanel, FrameHeader, FrameTitle, FrameDescription } from '@/components/reui/frame';
import { Badge } from '@/components/reui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@/components/ui/dialog';
import { toast, Toaster } from 'sonner';
import { api, useSnapshot } from './api';
import { Records } from './Records';
import { BusinessPage } from './BusinessPage';

type Product = {sku:string; name:string; risk:string; able:number|null; stock:number|null; monitor_qty:number|null; threshold:number; enabled:boolean};
type Inventory = {products:Product[]; enabled:boolean; connection:string; checked_at:number; source_busy:boolean};
function JobResult({id}:{id:string}) {
  const snapshot=useSnapshot<{replyText:string}>(`/api/orders/detail?id=${encodeURIComponent(id)}`,15000);
  return <div className="flex flex-col gap-3"><ErrorNotice message={snapshot.error} retry={snapshot.refresh}/>{snapshot.loading?<Skeleton className="h-20 w-full"/>:<p className="whitespace-pre-wrap break-words">{snapshot.data?.replyText}</p>}</div>;
}

type Job = {id:string; kind:string; number:string; shop:string; state:string; created:number; delivery:string};
type Orders = {configured?:boolean; shops?:{id:string;name:string}[]; jobs:Job[]; total:number; active:number; available:boolean; workerOnline:boolean; schedulePaused:boolean; serviceEnabled:boolean; stopRequested:boolean; stopError?:string; message?:string};
type Update = {phase:string; currentVersion:string; available:boolean; version?:string; notes?:string; error:string; received:number;total:number;lastResult?:string};
const emptyProducts:Product[]=[];
const emptyJobs:Job[]=[];
const names:Record<string,string>={queued:'排队中',running:'执行中',verified:'回读成功',completed:'执行完成',done:'执行完成',unknown:'结果未知',needs_review:'待核验',failed:'失败',pending:'待投递',sent:'已发送',delivered:'已投递',success:'执行完成',normal:'正常',low:'偏低',out:'缺货',disabled:'已暂停',waiting:'等待连接',online:'在线',stale:'连接过期',error:'连接异常',order:'订单匹配',today:'今日巡检',spu:'SPU 匹配',shipping:'发货监控'};
function StateBadge({value}:{value:string}) {
  const variant=['verified','normal','online','sent','delivered'].includes(value)?'success-light': ['failed','error','out'].includes(value)?'destructive-light':['unknown','needs_review','low','stale'].includes(value)?'warning-light':'secondary';
  return <Badge variant={variant}>{names[value]||value||'未知'}</Badge>;
}
function ErrorNotice({message,retry}:{message:string;retry:()=>void}) {
  return message?<div role="alert" className="flex flex-wrap items-center gap-3"><Badge variant="destructive-light">读取失败</Badge><span>{message}</span><Button size="sm" variant="outline" onClick={retry}>重试</Button></div>:null;
}
function useAction(refresh:()=>void) {
  const [busy,setBusy]=useState(false);
  const inFlight=useRef(false);
  const run=async(path:string,body:unknown)=>{
    if(inFlight.current)return;
    inFlight.current=true;setBusy(true);
    try {const result=await api<{message?:string}>(path,body);toast.success(result.message||'操作已提交，请查看最新状态');refresh();}
    catch(e){toast.error(e instanceof Error?e.message:'操作失败');}
    finally{inFlight.current=false;setBusy(false);}
  };
  return {busy,run};
}

function InventoryPage({onAddProduct}:{onAddProduct:()=>void}){
  const snapshot=useSnapshot<Inventory>('/api/inventory');
  const {busy,run}=useAction(snapshot.refresh);
  const [search,setSearch]=useState('');
  const rows=useMemo(()=>(snapshot.data?.products||emptyProducts).filter(p=>(p.name+' '+p.sku).toLowerCase().includes(search.toLowerCase())),[snapshot.data,search]);
  const columns=useMemo<ColumnDef<DataGridFeatures,Product>[]>(()=>[
    {accessorKey:'name',header:'监控商品',size:310},{accessorKey:'sku',header:'SKU',size:150},
    {accessorKey:'risk',header:'库存状态',cell:({row})=><StateBadge value={row.original.risk}/>},
    {accessorKey:'monitor_qty',header:'监控库存',cell:({row})=><span className="tabular-nums">{row.original.monitor_qty??'—'}</span>},
    {accessorKey:'able',header:'可销库存',cell:({row})=>row.original.able??'—'},
    {accessorKey:'threshold',header:'提醒阈值'}],[]);
  return <div className="flex min-w-0 flex-col gap-6"><ErrorNotice message={snapshot.error} retry={snapshot.refresh}/>
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">{[
      ['监控商品',String(snapshot.data?.products.length??'—')],['采集状态',names[snapshot.data?.connection||'waiting']],['上次采集',snapshot.data?.checked_at?new Date(snapshot.data.checked_at*1000).toLocaleString('zh-CN'):'尚未采集']
    ].map(([label,value])=><Frame key={label} spacing="sm"><FrameHeader><FrameDescription>{label}</FrameDescription></FrameHeader><FramePanel>{snapshot.loading?<Skeleton className="h-7 w-28"/>:<div className="text-lg font-semibold tabular-nums">{value}</div>}</FramePanel></Frame>)}</div>
    <Frame spacing="sm"><FrameHeader><div className="flex flex-wrap items-center justify-between gap-3"><div><FrameTitle>库存监控</FrameTitle><FrameDescription>库存以最近一次采集为准，未采集不显示为零。</FrameDescription></div><div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="outline" onClick={onAddProduct}><Plus/>添加产品</Button><Button size="sm" variant="outline" disabled={busy||!snapshot.data} onClick={()=>void run('/api/inventory/enabled',{enabled:!snapshot.data?.enabled})}>{snapshot.data?.enabled?<Pause/>:<Play/>}{snapshot.data?.enabled?'暂停监控':'恢复监控'}</Button><Button size="sm" disabled={busy||snapshot.data?.source_busy} onClick={()=>void run('/api/inventory/check',{})}><RefreshCw/>立即检查</Button></div></div></FrameHeader>
    <FramePanel><div className="flex flex-col gap-4"><label className="flex max-w-md flex-col gap-2"><span className="text-xs text-muted-foreground">搜索商品或 SKU</span><Input value={search} onChange={e=>setSearch(e.target.value)} placeholder="输入商品名称或 SKU"/></label><Records rows={rows} columns={columns} loading={snapshot.loading}/>{!rows.length&&!snapshot.loading&&<Button variant="outline" size="sm" onClick={()=>{setSearch('');snapshot.refresh();}}>清空搜索并刷新</Button>}</div></FramePanel></Frame>
  </div>;
}

function OrdersPage({onConfigure}:{onConfigure?:()=>void}){
  const [pagination,setPagination]=useState({pageIndex:0,pageSize:25});
  const snapshot=useSnapshot<Orders>(`/api/orders?page=${pagination.pageIndex}&size=${pagination.pageSize}`);
  const {busy,run}=useAction(snapshot.refresh);
  const [number,setNumber]=useState('');
  const [shop,setShop]=useState('');
  const [kind,setKind]=useState('order');
  const [selected,setSelected]=useState<Job|null>(null);
  // 店铺范围由本部门独立配置决定。
  const shopNames=useMemo(()=>Object.fromEntries((snapshot.data?.shops||[]).map(s=>[s.id,s.name])),[snapshot.data?.shops]);
  const columns=useMemo<ColumnDef<DataGridFeatures,Job>[]>(()=>[
    {accessorKey:'number',header:'订单 / SPU',size:220,cell:({row})=><Button variant="link" size="sm" onClick={()=>setSelected(row.original)}>{row.original.number||'当天已配置店铺巡检'}</Button>},
    {accessorKey:'kind',header:'任务类型',cell:({row})=>names[row.original.kind]||row.original.kind},
    {accessorKey:'shop',header:'店铺',cell:({row})=>(shopNames[row.original.shop]||row.original.shop||'自动识别')},
    {accessorKey:'state',header:'执行状态',cell:({row})=><StateBadge value={row.original.state}/>},
    {accessorKey:'delivery',header:'通知状态',cell:({row})=><StateBadge value={row.original.delivery}/>},
    {accessorKey:'created',header:'提交时间',size:180,cell:({row})=>new Date(row.original.created*1000).toLocaleString('zh-CN')}],[shopNames]);
  const act=(action:string)=>void run('/api/orders/action',{action});
  return <div className="flex min-w-0 flex-col gap-6"><ErrorNotice message={snapshot.error||snapshot.data?.stopError||""} retry={snapshot.refresh}/>
    <Frame spacing="sm"><FrameHeader><div className="flex flex-wrap items-center justify-between gap-3"><div><FrameTitle>订单匹配机器人</FrameTitle><FrameDescription>复用已有匹配队列和维护表，业务成功以 ERP 回读结果为准。</FrameDescription></div><Badge variant={snapshot.data?.workerOnline?'success-light':'secondary'}>{snapshot.data?.workerOnline?'工作进程在线':'工作进程离线'}</Badge></div></FrameHeader><FramePanel><div className="flex flex-col gap-4"><div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" onClick={onConfigure}>订单匹配设置</Button><Button size="sm" variant="outline" disabled={busy||!snapshot.data?.configured} onClick={()=>act(snapshot.data?.serviceEnabled||(snapshot.data?.stopRequested&&snapshot.data?.workerOnline)?'stop':'start')}>{snapshot.data?.stopRequested&&snapshot.data?.workerOnline?'继续等待停止':snapshot.data?.serviceEnabled?'停止服务':'启动服务'}</Button><Button size="sm" variant="outline" disabled={busy||!snapshot.data?.available} onClick={()=>act(snapshot.data?.schedulePaused?'resume':'pause')}>{snapshot.data?.schedulePaused?<Play/>:<Pause/>}{snapshot.data?.schedulePaused?'恢复自动巡检':'暂停自动巡检'}</Button><Button size="sm" disabled={busy||!snapshot.data?.workerOnline||!snapshot.data?.serviceEnabled} onClick={()=>act('patrol')}>巡检今天</Button><span className="self-center text-xs text-muted-foreground">{snapshot.data?.stopRequested?'正在等待当前任务安全结束':snapshot.data?.schedulePaused?'自动巡检已暂停':'按原设置的间隔巡检'}</span></div>
      <form className="flex flex-wrap items-end gap-3" onSubmit={e=>{e.preventDefault();void run('/api/orders/action',{action:kind,number,shop});}}>
      <label className="flex flex-col gap-2"><span className="text-xs">类型</span><Select value={kind} onValueChange={v=>{if(v)setKind(v);}} items={{order:"订单匹配",spu:"SPU 匹配",number:"号码自动识别"}}><SelectTrigger size="sm" aria-label="匹配类型"><SelectValue/></SelectTrigger><SelectContent><SelectItem value="order">订单匹配</SelectItem><SelectItem value="spu">SPU 匹配</SelectItem><SelectItem value="number">号码自动识别</SelectItem></SelectContent></Select></label>
      <label className="flex flex-col gap-2"><span className="text-xs">店铺</span><Select value={shop} onValueChange={v=>setShop(v||"")} items={{"":"自动识别",...shopNames}}><SelectTrigger size="sm" aria-label="匹配店铺"><SelectValue/></SelectTrigger><SelectContent><SelectItem value="">自动识别</SelectItem>{(snapshot.data?.shops||[]).map(s=><SelectItem key={s.id} value={s.id}>{s.name}</SelectItem>)}</SelectContent></Select></label>
      <label className="flex min-w-0 basis-full flex-col gap-2 sm:flex-1 sm:basis-64"><span className="text-xs">完整订单号或商品编号</span><Input required value={number} onChange={e=>setNumber(e.target.value)} placeholder="保留完整编号，不按数字长度猜测类型"/></label><Button size="sm" type="submit" disabled={busy||!number.trim()||!snapshot.data?.workerOnline||!snapshot.data?.serviceEnabled}>提交匹配</Button></form>
      {!snapshot.data?.available&&<p className="text-muted-foreground">{snapshot.data?.message||'正在读取机器人状态'}</p>}</div></FramePanel></Frame>
    <Frame spacing="sm"><FrameHeader><div className="flex items-center justify-between gap-3"><FrameTitle>任务记录</FrameTitle><Button variant="outline" size="sm" onClick={snapshot.refresh}><RefreshCw/>刷新</Button></div></FrameHeader><FramePanel><Records rows={snapshot.data?.jobs||emptyJobs} columns={columns} loading={snapshot.loading} total={snapshot.data?.total||0} pagination={pagination} onPaginationChange={setPagination}/>{snapshot.data?.total===0&&<Button size="sm" variant="outline" onClick={snapshot.refresh}>读取任务记录</Button>}</FramePanel></Frame>
    <Dialog open={!!selected} onOpenChange={open=>{if(!open)setSelected(null);}}><DialogContent><DialogHeader><DialogTitle>任务详情</DialogTitle><DialogDescription>排队和执行完成不等于 ERP 回读成功。</DialogDescription></DialogHeader>{selected&&<div className="flex flex-col gap-3 break-all"><span>任务编号：{selected.id}</span><span>业务编号：{selected.number||'当天巡检'}</span><div className="flex gap-2"><StateBadge value={selected.state}/><StateBadge value={selected.delivery}/></div><JobResult id={selected.id}/></div>}</DialogContent></Dialog>
  </div>;
}

function UpdatesPage(){
  const snapshot=useSnapshot<Update>('/api/update',1500);
  const info=useSnapshot<{autoCheck:boolean;version:string}>('/api/info',30000);
  const {busy,run}=useAction(snapshot.refresh);
  const state=snapshot.data;
  const working=busy||['checking','downloading','preparing','installing'].includes(state?.phase||'');
  return <Frame spacing="sm"><FrameHeader><FrameTitle>软件更新</FrameTitle><FrameDescription>检查新版本，一键下载、校验并重启。业务数据保留在原位置。</FrameDescription></FrameHeader><FramePanel><div className="flex flex-col gap-6"><ErrorNotice message={snapshot.error} retry={snapshot.refresh}/><div className="flex flex-wrap items-center gap-3"><span>当前版本</span><Badge variant="outline">v{state?.currentVersion||info.data?.version||'—'}</Badge>{state?.available&&<Badge variant="info-light">可更新至 v{state.version}</Badge>}</div><label className="flex items-center gap-2"><input type="checkbox" checked={info.data?.autoCheck??true} onChange={e=>void api('/api/update/preferences',{autoCheck:e.target.checked}).then(info.refresh).catch(e=>toast.error(e.message))}/>启动时自动检查更新</label>
      <div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={working} onClick={()=>void run('/api/update/check',{})}><RefreshCw/>{state?.phase==='checking'?'正在检查…':'检查更新'}</Button><Button size="sm" disabled={working||!state?.available} onClick={()=>void run('/api/update/install',{})}><Download/>{state?.phase==='installing'?'正在安装…':'一键更新并重启'}</Button></div>
      {state?.phase==='downloading'&&<div role="status" className="flex flex-col gap-2"><progress className="w-full" value={state.received} max={state.total||1}/><span className="text-xs tabular-nums">已下载 {(state.received/1024/1024).toFixed(1)} / {(state.total/1024/1024).toFixed(1)} MB</span></div>}
      {state?.error&&<div role="alert"><Badge variant="destructive-light">更新未完成</Badge><p className="mt-2">{state.error}</p></div>}
      {state?.phase==='ready'&&!state.available&&<p role="status">当前已是最新版本。</p>}
      {state?.lastResult&&<p role="status">上次更新：{state.lastResult}</p>}
      {state?.notes&&<section className="flex flex-col gap-3"><h2 className="font-semibold">更新说明</h2><p className="whitespace-pre-wrap text-muted-foreground">{state.notes}</p></section>}
    </div></FramePanel></Frame>;
}

function ConsoleLayout(){
  const [page,setPage]=useState('inventory');
  const {setOpenMobile}=useSidebar();
  // 快捷入口复用商品管理表单，普通导航不自动打开表单。
  const [openAddProduct,setOpenAddProduct]=useState(false);
  const navigate=(id:string,addProduct=false)=>{setOpenAddProduct(addProduct);setPage(id);setOpenMobile(false);};
  const [dark,setDark]=useState(()=>localStorage.getItem('console-theme')==='dark');
  const autoChecked=useRef(false);
  const info=useSnapshot<{autoCheck:boolean;version:string;business:boolean;workspace?:{department:string;name:string}}>('/api/info',30000);
  useEffect(()=>{document.documentElement.classList.toggle('dark',dark);localStorage.setItem('console-theme',dark?'dark':'light');},[dark]);
  useEffect(()=>{if(info.data&&!autoChecked.current){autoChecked.current=true;if(info.data.autoCheck)void api('/api/update/check',{}).catch(()=>{});}},[info.data]);
  // 导航顺序按工作台、业务管理、设置分组；货源汇报保留在钉钉货源监控内。
  const groups=[
    {title:'工作台',items:[{id:'inventory',title:'配件库存监控',icon:Boxes},{id:'orders',title:'订单巡检匹配',icon:ListChecks},{id:'douyin',title:'前台库存监控',icon:PackageSearch}]},
    {title:'业务管理',items:[{id:'dingtalk',title:'钉钉货源监控',icon:Bell},{id:'profit',title:'ERP调价监控',icon:ChartNoAxesCombined},{id:'events',title:'库存提醒记录',icon:Bell}]},
    {title:'设置',items:[{id:'monitor',title:'管理监控商品',icon:Boxes},{id:'settings',title:'连接与通知设置',icon:Settings},{id:'order-settings',title:'订单机器人设置',icon:ListChecks}]},
  ];
  const pages=groups.flatMap(group=>group.items);
  return <><Sidebar><SidebarHeader><div className="flex items-center gap-3 p-2"><img src="/app-icon.ico" alt="" className="size-8"/><div className="flex flex-col gap-0.5"><strong>后台库存监控</strong><span className="text-xs text-muted-foreground">库存 · 订单 · 运营</span></div></div></SidebarHeader><SidebarContent>{groups.map(group=><SidebarGroup key={group.title}><SidebarGroupLabel>{group.title}</SidebarGroupLabel><SidebarMenu>{group.items.map(item=><SidebarMenuItem key={item.id}><SidebarMenuButton aria-current={page===item.id?'page':undefined} isActive={page===item.id} onClick={()=>navigate(item.id)}><item.icon/><span>{item.title}</span></SidebarMenuButton></SidebarMenuItem>)}</SidebarMenu></SidebarGroup>)}</SidebarContent><SidebarFooter><SidebarMenu><SidebarMenuItem><SidebarMenuButton aria-current={page==='updates'?"page":undefined} isActive={page==='updates'} onClick={()=>navigate('updates')}><Download/><span>软件更新</span></SidebarMenuButton></SidebarMenuItem></SidebarMenu><div className="flex items-center justify-between gap-2 p-2"><span className="text-xs text-muted-foreground">v{info.data?.version||'—'} · {info.data?.workspace?.department||info.data?.workspace?.name||'本机工作台'}</span><Button variant="ghost" size="icon-sm" aria-label={dark?'切换浅色':'切换深色'} onClick={()=>setDark(v=>!v)}>{dark?<Sun/>:<Moon/>}</Button></div></SidebarFooter></Sidebar><SidebarInset><header className="flex items-center gap-3 border-b p-4"><SidebarTrigger aria-label="展开或收起导航"/><div className="flex flex-col gap-0.5"><h1 className="text-lg font-semibold tracking-tight">{[...pages,{id:'updates',title:'软件更新'}].find(p=>p.id===page)?.title}</h1><p className="text-xs text-muted-foreground">统一查看状态，按任务处理异常</p></div></header><div className="flex min-h-0 min-w-0 flex-1 flex-col p-4 md:p-6">{page==='inventory'?<InventoryPage onAddProduct={()=>navigate('monitor',true)}/>:page==='orders'?<OrdersPage onConfigure={()=>navigate('order-settings')}/>:page==='updates'?<UpdatesPage/>:<BusinessPage key={page} module={page} openAddProduct={openAddProduct}/>}</div></SidebarInset><Toaster richColors theme={dark?'dark':'light'}/></>;
}

// 导航由同一布局管理，移动侧栏选中后关闭，更新入口固定在页脚。
export default function App(){return <SidebarProvider><ConsoleLayout/></SidebarProvider>;}
