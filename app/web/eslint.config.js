import js from '@eslint/js';
import ts from 'typescript-eslint';
import globals from 'globals';
// 第三方组件保持官方代码；检查本轮业务页面与请求逻辑。
export default ts.config({ignores:['node_modules/**','src/components/**','src/hooks/**']},js.configs.recommended,...ts.configs.recommended,
  {files:['src/**/*.{ts,tsx}','vite.config.ts'],languageOptions:{globals:{...globals.browser,...globals.node}},rules:{'@typescript-eslint/no-unused-vars':['error',{argsIgnorePattern:'^_'}]}});
