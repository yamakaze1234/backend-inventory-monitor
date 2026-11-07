import { useEffect, useRef, useState } from 'react';
import { useSidebar } from '@/components/ui/sidebar';
import { Button } from '@/components/ui/button';
import { api } from './api';

type Host = {postMessage:(message:unknown)=>void};
function host():Host|undefined{return (window as unknown as {chrome?:{webview?:Host}}).chrome?.webview;}

/** 只把当前内容区的边界发给宿主，业务页面沿用原服务实例和操作。 */
export function NativePage({module,available}:{module:string;available:boolean}){
  const region=useRef<HTMLDivElement>(null);
  const [error,setError]=useState('');
  const [ready,setReady]=useState(false);
  const [retry,setRetry]=useState(0);
  const {openMobile}=useSidebar();
  useEffect(()=>{
    setReady(false);setError('');
    if(!available||!host())return;
    const controller=new AbortController();
    void api('/api/native',{module},controller.signal).then(()=>setReady(true)).catch(e=>{
      if(!controller.signal.aborted)setError(e instanceof Error?e.message:'页面暂时无法加载');
    });
    return ()=>controller.abort();
  },[module,available,retry]);
  useEffect(()=>{
    const bridge=host();
    if(!bridge)return;
    let frame=0;
    const send=()=>{
      const rect=region.current?.getBoundingClientRect();
      bridge.postMessage({type:'native-region',visible:ready&&!openMobile&&!document.hidden,
        x:rect?.x||0,y:rect?.y||0,width:rect?.width||0,height:rect?.height||0,
        viewportWidth:innerWidth,viewportHeight:innerHeight});
    };
    // 侧栏展开动画会改变左边界；仅在布局变化时逐帧跟随，不轮询业务数据。
    const track=()=>{cancelAnimationFrame(frame);const until=performance.now()+250;
      const tick=()=>{send();if(performance.now()<until)frame=requestAnimationFrame(tick);};tick();};
    const observer=new ResizeObserver(track);
    if(region.current)observer.observe(region.current);
    window.addEventListener('resize',track);
    window.addEventListener('scroll',track,true);
    document.addEventListener('visibilitychange',send);
    track();
    return ()=>{cancelAnimationFrame(frame);observer.disconnect();window.removeEventListener('resize',track);
      window.removeEventListener('scroll',track,true);document.removeEventListener('visibilitychange',send);
      bridge.postMessage({type:'native-region',visible:false});};
  },[ready,openMobile]);
  return <div ref={region} data-native-module={module} className="min-h-[420px] min-w-0 flex-1" style={{height:'calc(100dvh - 140px)'}}>
    {!available||!host()?<p className="text-sm text-muted-foreground">此功能在桌面软件的当前内容区使用。</p>:
      error?<div role="alert" className="flex items-center gap-3"><span>{error}</span><Button variant="outline" onClick={()=>setRetry(v=>v+1)}>重试</Button></div>:
      !ready?<p role="status" className="text-sm text-muted-foreground">正在加载业务页面…</p>:null}
  </div>;
}
