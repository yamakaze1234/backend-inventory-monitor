import { createRoot } from 'react-dom/client';
import { TooltipProvider } from '@/components/ui/tooltip';
import App from './App';
import './index.css';
// 页面只在浏览器侧挂载一次，避免开发双挂载触发重复后台检查。
createRoot(document.getElementById('root')!).render(<TooltipProvider><App/></TooltipProvider>);
