"""由用户选择维护表和浏览器；文件检查在后台运行，不阻塞窗口绘制。"""
from pathlib import Path
import json
import os
import queue
import re
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from inventory_theme import BG, INK, MUTED, button


def background(win, job, done, failed):
    """后台只读计算通过队列交回 Tk 主线程，窗口关闭后不再更新控件。"""
    results = queue.Queue()
    def run():
        try:
            results.put((True, job()))
        except Exception as error:
            results.put((False, str(error) if isinstance(error, ValueError) else '读取失败，请检查文件或浏览器扩展连接'))
    def poll():
        if not win.winfo_exists():
            return
        try:
            ok, value = results.get_nowait()
        except queue.Empty:
            win.after(100, poll); return
        (done if ok else failed)(value)
    threading.Thread(target=run, daemon=True, name='douyin-settings-read').start()
    win.after(100, poll)


def window(panel, title):
    win = tk.Toplevel(panel); win.title(title); win.configure(bg=BG); win.transient(panel.winfo_toplevel())
    win.geometry('650x380'); win.minsize(600, 340)
    body = tk.Frame(win, bg=BG, padx=20, pady=18); body.pack(fill='both', expand=True)
    return win, body


def maintenance_dialog(panel):
    config = panel.config()
    if not config:
        return
    win, body = window(panel, '主机维护表 · ' + config['name'])
    path = tk.StringVar(win, config['maintenance_path']); sheet = tk.StringVar(win, config['maintenance_sheet'])
    info = {'shop_id': config['maintenance_shop_id']}
    hint = tk.StringVar(win, '选择本店正式维护表，仅读取已保存内容，不修改文件、不运行宏。')
    tk.Label(body, text='维护表文件', bg=BG, fg=INK).pack(anchor='w')
    line = tk.Frame(body, bg=BG); line.pack(fill='x', pady=8)
    ttk.Entry(line, textvariable=path, state='readonly').pack(side='left', fill='x', expand=True, padx=(0, 8))
    def choose():
        selected = filedialog.askopenfilename(title='选择本店主机维护表', parent=win,
            filetypes=[('主机维护表', '*.xlsb *.xlsx *.xlsm')])
        if not selected:
            return
        path.set(selected); sheet.set(''); save.config(state='disabled'); pick.config(state='disabled')
        hint.set('正在检查工作表和店铺编号…')
        from douyin_maintenance import inspect_maintenance, KNOWN_SHOPS
        def done(data):
            pick.config(state='normal')
            if KNOWN_SHOPS.get(config['name'], data['shop_id']) != data['shop_id']:
                hint.set('店铺编号不符，请选择当前店铺的维护表'); return
            path.set(data['path']); info['shop_id'] = data['shop_id']; choices.config(values=data['sheets'])
            if len(data['sheets']) == 1:
                sheet.set(data['sheets'][0])
            elif config['maintenance_sheet'] in data['sheets']:
                sheet.set(config['maintenance_sheet'])
            hint.set('表内店铺编号：' + data['shop_id'] + '。请选择实际维护的配置工作表。')
            save.config(state='normal')
        def failed(reason):
            pick.config(state='normal'); hint.set(reason)
        background(win, lambda: inspect_maintenance(selected), done, failed)
    pick = button(line, '选择文件', choose, primary=True); pick.pack(side='right')
    tk.Label(body, text='配置工作表', bg=BG, fg=INK).pack(anchor='w', pady=(10, 6))
    choices = ttk.Combobox(body, textvariable=sheet, state='readonly', values=[sheet.get()] if sheet.get() else [])
    choices.pack(fill='x')
    tk.Label(body, textvariable=hint, bg=BG, fg=MUTED, wraplength=590, justify='left').pack(anchor='w', pady=16)
    tk.Label(body, text='配件按商品 ID＋SKU ID 匹配；重复、缺失或编号无效均标为待核对。\n维护表更新后请先保存，程序不会读取未保存编辑。',
             bg=BG, fg=MUTED, justify='left').pack(anchor='w')
    def commit():
        if not path.get() or not sheet.get() or not info.get('shop_id'):
            hint.set('请先选择文件和配置工作表'); return
        panel.runtime.submit('maintenance:' + json.dumps(dict(path=path.get(), sheet=sheet.get(), shop_id=info['shop_id']), ensure_ascii=False), config['id'])
        win.destroy()
    save = button(body, '保存本店设置', commit, primary=True); save.pack(side='bottom', anchor='e', pady=12)
    if not path.get():
        save.config(state='disabled')
    return win


def login_dialog(panel):
    """专用浏览器自动隔离店铺；旧扩展连接保留为兼容选项。"""
    config = panel.config()
    if not config:
        return
    win, body = window(panel, '店铺登录设置 · ' + config['name'])
    win.geometry('680x530'); win.minsize(630, 500)
    browser = tk.StringVar(win, 'Edge' if config['browser'] == 'edge' else 'Chrome')
    modes = {'独立店铺浏览器（推荐）': 'managed', '现有浏览器扩展（兼容）': 'extension'}
    mode = tk.StringVar(win, next(k for k,v in modes.items() if v == config.get('connection_mode','extension')))
    profile = tk.StringVar(win, config['browser_profile'])
    hint = tk.StringVar(win, '')
    win.connection_mode = mode
    actions = tk.Frame(body, bg=BG); actions.pack(side='bottom', fill='x', pady=(12,0))
    tk.Label(body, text='连接方式', bg=BG, fg=INK).pack(anchor='w')
    ttk.Combobox(body, textvariable=mode, values=list(modes), state='readonly').pack(fill='x', pady=(6,12))
    tk.Label(body, text='浏览器', bg=BG, fg=INK).pack(anchor='w')
    ttk.Combobox(body, textvariable=browser, values=['Chrome','Edge'], state='readonly').pack(fill='x', pady=(6,12))
    native = tk.Frame(body, bg=BG)
    tk.Label(native, text='每店自动分配独立窗口和本地目录，无需安装扩展或填写连接标识。\n首次在本店窗口手动登录，之后可继续使用浏览器保存的登录状态。\n登录过期时重新登录，再校验连接。',
             bg=BG, fg=MUTED, justify='left', wraplength=620).pack(anchor='w')
    location = tk.Label(native, bg=BG, fg=MUTED, justify='left', wraplength=620)
    location.pack(anchor='w', pady=(12,0))
    extension = tk.Frame(body, bg=BG)
    tk.Label(extension, text='本店扩展连接标识（多店必须各不相同）', bg=BG, fg=INK).pack(anchor='w')
    line = tk.Frame(extension, bg=BG); line.pack(fill='x', pady=8)
    choices = ttk.Combobox(line, textvariable=profile); choices.pack(side='left', fill='x', expand=True, padx=(0,8))
    def refresh():
        from douyin_export import cli_command
        def read():
            result = subprocess.run(cli_command() + ['profile','list'], capture_output=True, text=True, encoding='utf-8', timeout=15,
                                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode:
                raise ValueError('扩展未连接，请检查 Chrome / Edge 的 OpenCLI 扩展')
            return re.findall(r'^\s+([\w-]+)\s+—\s+connected', result.stdout, re.M)
        background(win, read, lambda ids: choices.config(values=ids), hint.set)
    button(line, '刷新连接', refresh).pack(side='right')
    tk.Label(extension, text='沿用已有 OpenCLI 扩展连接。不同店铺需使用不同浏览器用户配置。',
             bg=BG, fg=MUTED, justify='left', wraplength=620).pack(anchor='w')
    hint_label = tk.Label(body, textvariable=hint, bg=BG, fg=MUTED, justify='left', wraplength=620)
    hint_label.pack(side='bottom', fill='x', pady=(12,0))
    controls = []
    def busy(value):
        for control in controls:
            control.config(state='disabled' if value else 'normal')
    def response(reply):
        busy(True); hint.set('正在处理本店设置，请稍候…')
        def wait():
            try:
                result = reply.get(timeout=60)
            except queue.Empty:
                raise ValueError('操作仍在队列中，请在店铺管理查看状态；未自动重复提交') from None
            if not result.get('ok'):
                raise ValueError(result.get('message','操作未完成'))
            return result.get('message','本店连接已校验，可返回抖店页面立即检查')
        def done(text):
            busy(False); hint.set(text)
        background(win, wait, done, done)
    def settings(open_browser=False):
        current_mode = modes[mode.get()]
        value = dict(browser=browser.get().lower(),profile=profile.get().strip(),mode=current_mode)
        reply = queue.Queue(maxsize=1)
        panel.runtime.submit(dict(kind='browser_settings',value=value,open=open_browser and current_mode=='managed',reply=reply),config['id'])
        response(reply)
        if open_browser and current_mode == 'extension':
            from douyin_browser import find_browser
            try:
                subprocess.Popen([str(find_browser(value['browser'])), 'https://fxg.jinritemai.com/ffa/g/list'])
            except ValueError as error:
                hint.set(str(error))
    def connect():
        current = panel.runtime.store.store(config['id'])
        selected_profile = profile.get().strip() if modes[mode.get()] == 'extension' else ''
        if (current['browser'], current['browser_profile'], current.get('connection_mode','extension')) != (browser.get().lower(), selected_profile, modes[mode.get()]):
            hint.set('连接设置尚未保存，请先点击“保存设置”或“打开本店浏览器”。')
            return
        reply = queue.Queue(maxsize=1)
        panel.runtime.submit(dict(kind='connect',reply=reply), config['id'])
        response(reply)
    for title, command in [('保存设置', settings), ('打开本店浏览器', lambda: settings(True)), ('校验连接', connect), ('关闭', win.destroy)]:
        control = button(actions, title, command, primary=title=='打开本店浏览器', padx=10)
        control.pack(side='left', padx=(0,8)); controls.append(control)
    def render(*_):
        native.pack_forget(); extension.pack_forget()
        selected = native if modes[mode.get()] == 'managed' else extension
        selected.pack(fill='x', before=hint_label)
        if selected is native:
            from douyin_browser import ShopBrowser
            location.config(text='本店专用目录：' + str(ShopBrowser(panel.runtime.store.root,config['id'],browser.get().lower()).user_data))
        hint.set('打开本店浏览器 → 手动登录 → 校验连接。切换连接方式需在新窗口重新登录一次。')
    mode.trace_add('write',render); browser.trace_add('write',render)
    win.native_frame, win.extension_frame, win.login_controls = native, extension, controls
    render()
    return win
