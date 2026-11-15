import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { fileURLToPath, URL } from 'node:url';
// 产物放入桌面资源目录，由本机服务提供同源访问。
export default defineConfig({ plugins:[react(),tailwindcss()], resolve:{alias:{'@':fileURLToPath(new URL('./src',import.meta.url))}},
  build:{outDir:'../ui/console',emptyOutDir:false,rollupOptions:{output:{manualChunks(id){
    // 稳定组件库单独缓存，业务页面更新不要求重新解析全部表格代码。
    if(id.includes('node_modules')) {
      if(/node_modules\/(react|react-dom|scheduler)\//.test(id)) return 'react-runtime';
      if(id.includes('/@base-ui/')) return 'base-controls';
      if(id.includes('/@tanstack/')) return 'table-engine';
      return 'vendor';
    }
  }}}} });
