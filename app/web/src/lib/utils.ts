import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';
// 合并组件类名时遵循 ReUI 自带的样式优先级。
export function cn(...inputs: ClassValue[]) { return twMerge(clsx(inputs)); }
