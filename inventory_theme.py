"""Shared desktop visuals; no service, network or credential dependencies."""
import tkinter as tk
from tkinter import ttk

BG, INK, MUTED, BLUE = '#F1F5FB', '#223550', '#586D8C', '#285EB9'
BORDER = '#C3D3EB'


def sync_tree(tree, rows):
    """Update changed rows in place, retaining selection and the viewport."""
    rows = list(rows)
    desired = [str(row[0]) for row in rows]
    wanted = set(desired)
    current = list(tree.get_children())
    for iid in current:
        if iid not in wanted:
            tree.delete(iid)
    existing = set(current) & wanted
    for iid, values, tags in rows:
        iid = str(iid)
        if iid not in existing:
            tree.insert('', 'end', iid=iid, values=values, tags=tags)
        else:
            item = tree.item(iid)
            if tuple(map(str, item['values'])) != tuple(map(str, values)) or tuple(item['tags']) != tuple(tags):
                tree.item(iid, values=values, tags=tags)
    if list(tree.get_children()) != desired:
        for position, iid in enumerate(desired):
            tree.move(iid, '', position)


def install_theme(widget):
    root = widget._root()
    if getattr(root, '_inventory_theme_ready', False):
        return
    def tree_wheel(event):
        tree = event.widget
        remainder = getattr(tree, '_wheel_remainder', 0.0) - event.delta / 120
        steps = int(remainder)
        tree._wheel_remainder = remainder - steps
        if steps:
            tree.yview_scroll(steps, 'units')
        return 'break'
    root.bind_class('Treeview', '<MouseWheel>', tree_wheel)
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', font=('Microsoft YaHei UI', 10), background='white', foreground=INK)
    style.configure('TFrame', background='white')
    style.configure('TLabel', background='white', foreground=INK)
    for name in ('TEntry', 'TCombobox', 'TSpinbox'):
        style.configure(name, padding=7, fieldbackground='white', bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, arrowsize=12)
        style.map(name, bordercolor=[('focus', BLUE)],
                  fieldbackground=[('readonly', '#F5F6FA'), ('disabled', '#F5F6FA')],
                  foreground=[('disabled', MUTED)])
    style.configure('TButton', padding=(14, 9), borderwidth=0, relief='flat',
                    background='#EFF4FC', foreground=INK)
    style.map('TButton', background=[('disabled', '#F1F3F7'), ('active', '#E3EDFC')],
              foreground=[('disabled', '#9AA4B3')])
    style.configure('Primary.TButton', background=BLUE, foreground='white')
    style.map('Primary.TButton', background=[('disabled', '#E5ECF7'), ('active', '#204E9C')],
              foreground=[('disabled', '#8C82AF'), ('!disabled', 'white')])
    for name in ('TCheckbutton', 'TRadiobutton'):
        style.configure(name, padding=(0, 5), background='white', foreground=INK)
        style.map(name, background=[('active', '#EFF4FC')], indicatorbackground=[('selected', BLUE)])
    # Replace the platform's small cross/tick indicators with clear, consistent marks.
    from PIL import Image, ImageDraw, ImageTk
    root._inventory_indicator_images = []
    # Stretchable rounded field images preserve native text selection and editing.
    fields = []
    for fill, outline in [('white', BORDER), ('white', BLUE), ('#F4F7FC', BORDER)]:
        bitmap = Image.new('RGBA', (112, 112), (255, 255, 255, 0))
        draw = ImageDraw.Draw(bitmap)
        draw.rounded_rectangle((2, 2, 109, 109), radius=26, fill=fill, outline=outline, width=4)
        fields.append(ImageTk.PhotoImage(bitmap.resize((28, 28), Image.Resampling.LANCZOS), master=root))
    root._inventory_indicator_images.extend(fields)
    for control in ('Entry', 'Combobox', 'Spinbox'):
        element = 'Rounded.' + control + '.field'
        style.element_create(element, 'image', fields[0], ('focus', fields[1]),
                             ('readonly', fields[2]), ('disabled', fields[2]), border=8, sticky='nswe')
        content = [(control + '.padding', {'sticky': 'nswe', 'children': [(control + '.textarea', {'sticky': 'nswe'})]})]
        if control == 'Combobox':
            content.insert(0, ('Combobox.downarrow', {'side': 'right', 'sticky': 'ns'}))
        elif control == 'Spinbox':
            content.insert(0, ('null', {'side': 'right', 'sticky': '', 'children': [
                ('Spinbox.uparrow', {'side': 'top', 'sticky': 'e'}),
                ('Spinbox.downarrow', {'side': 'bottom', 'sticky': 'e'})]}))
        style.layout('T' + control, [(element, {'sticky': 'nswe', 'children': content})])
        style.configure('T' + control, padding=(3, 1), arrowcolor=MUTED)
    for kind, control in [('check', 'Checkbutton'), ('radio', 'Radiobutton')]:
        images = []
        for selected in (False, True):
            bitmap = Image.new('RGBA', (80, 80), (255, 255, 255, 0))
            draw = ImageDraw.Draw(bitmap)
            fill = BLUE if selected else 'white'
            if kind == 'check':
                draw.rounded_rectangle((8, 8, 68, 68), radius=12, fill=fill, outline=BLUE if selected else '#A6B2C4', width=5)
                if selected:
                    draw.line([(21, 38), (33, 50), (55, 25)], fill='white', width=7, joint='curve')
            else:
                draw.ellipse((8, 8, 68, 68), fill='white', outline=BLUE if selected else '#A6B2C4', width=5)
                if selected:
                    draw.ellipse((23, 23, 53, 53), fill=BLUE)
            photo = ImageTk.PhotoImage(bitmap.resize((18, 18), Image.Resampling.LANCZOS), master=root)
            images.append(photo)
            root._inventory_indicator_images.append(photo)
        element = 'Inventory.' + control + '.indicator'
        style.element_create(element, 'image', images[0], ('selected', images[1]), width=22, sticky='w')
        style.layout('T' + control, [(control + '.padding', {'sticky': 'nswe', 'children': [
            (element, {'side': 'left', 'sticky': 'w'}),
            (control + '.focus', {'side': 'left', 'sticky': 'w', 'children': [(control + '.label', {'sticky': 'nswe'})]})]})])
    style.configure('TLabelframe', background='white', bordercolor=BORDER, relief='solid', borderwidth=1)
    style.configure('TLabelframe.Label', background='white', foreground=INK,
                    font=('Microsoft YaHei UI', 11, 'bold'))
    style.configure('TNotebook', background='white', borderwidth=0, bordercolor='white', lightcolor='white', darkcolor='white')
    style.configure('TNotebook.Tab', padding=(16, 9), background=BG, foreground=MUTED)
    style.map('TNotebook.Tab', background=[('selected', 'white')], foreground=[('selected', BLUE)])
    style.layout('Page.TNotebook.Tab', [])
    style.configure('Treeview', background='white', fieldbackground='white', rowheight=38,
                    borderwidth=0, relief='flat', bordercolor=BORDER, font=('Microsoft YaHei UI', 10))
    style.configure('Treeview.Heading', background='#F6F7FA', foreground=MUTED,
                    font=('Microsoft YaHei UI', 9, 'bold'), relief='flat', padding=(10, 10))
    style.map('Treeview', background=[('selected', '#E7EFFD')], foreground=[('selected', '#1E4F9B')])
    style.map('Treeview.Heading', background=[('active', '#ECEFF5')])
    root._inventory_theme_ready = True


def button(parent, text, command, primary=False, **kwargs):
    return RoundedButton(parent, text, command, primary, **kwargs)


class RoundedButton(tk.Button):
    """Native button interaction with an antialiased, rounded background."""
    def __init__(self, parent, text, command, primary=False, **kwargs):
        self._fill = kwargs.pop('bg', BLUE if primary else 'white')
        self._primary = primary
        self._padx, self._pady = kwargs.pop('padx', 16), kwargs.pop('pady', 9)
        self._hover = self._focused = False
        self._ready = False
        self._image_key = None
        options = dict(fg='white' if primary else INK, relief='flat', bd=0,
                       activeforeground='white' if primary else INK,
                       disabledforeground='#99A6B9', cursor='hand2', font=('Microsoft YaHei UI', 10),
                       compound='center', takefocus=1)
        options.update(kwargs)
        surface = parent.cget('bg') if 'bg' in parent.keys() else 'white'
        options.update(bg=surface, activebackground=surface, highlightthickness=0, padx=0, pady=0)
        super().__init__(parent, text=text, command=command, **options)
        self._ready = True
        self.bind('<Enter>', lambda e: self._set_state(hover=True))
        self.bind('<Leave>', lambda e: self._set_state(hover=False))
        self.bind('<FocusIn>', lambda e: self._set_state(focused=True))
        self.bind('<FocusOut>', lambda e: self._set_state(focused=False))
        self.bind('<Configure>', lambda e: self._paint())
        self._paint()

    def configure(self, cnf=None, **kwargs):
        if not getattr(self, '_ready', False):
            return super().configure(cnf, **kwargs)
        if isinstance(cnf, dict):
            kwargs = {**cnf, **kwargs}
            cnf = None
        if cnf is not None or not kwargs:
            return super().configure(cnf, **kwargs)
        if 'bg' in kwargs:
            self._fill = kwargs.pop('bg')
        if 'padx' in kwargs:
            self._padx = kwargs.pop('padx')
        if 'pady' in kwargs:
            self._pady = kwargs.pop('pady')
        for key in ('highlightbackground', 'highlightcolor', 'highlightthickness', 'activebackground'):
            kwargs.pop(key, None)
        result = super().configure(**kwargs) if kwargs else None
        self._paint()
        return result

    config = configure

    def _set_state(self, **values):
        for key, value in values.items():
            setattr(self, '_' + key, value)
        self._paint()

    def _paint(self):
        from tkinter import font
        from PIL import Image, ImageDraw, ImageTk
        face = font.Font(self, font=self.cget('font'))
        lines = str(self.cget('text')).splitlines() or ['']
        width = max(face.measure(line) for line in lines) + 2 * int(self._padx) + 2
        height = face.metrics('linespace') * len(lines) + 2 * int(self._pady) + 2
        disabled = str(self.cget('state')) == 'disabled'
        fill = '#F5F7FB' if disabled else self._fill
        if self._hover and not disabled:
            fill = '#204E9C' if self._primary else '#EDF3FD'
        outline = BLUE if self._focused else (fill if self._primary else BORDER)
        key = (width, height, fill, outline)
        if key == self._image_key:
            return
        self._image_key = key
        bitmap = Image.new('RGBA', (width * 3, height * 3), (255, 255, 255, 0))
        draw = ImageDraw.Draw(bitmap)
        draw.rounded_rectangle((1, 1, width * 3 - 2, height * 3 - 2), radius=21,
                               fill=fill, outline=outline, width=3)
        self._background_image = ImageTk.PhotoImage(bitmap.resize((width, height), Image.Resampling.LANCZOS), master=self)
        super().configure(image=self._background_image)


def wrap_label(label, margin=0):
    label.bind('<Configure>', lambda e: label.configure(wraplength=max(120, e.width - margin)), add='+')
    return label


def section(parent, title, description=''):
    shell = RoundedCard(parent)
    shell.pack(fill='x', pady=(0, 16))
    card = shell.content
    tk.Label(card, text=title, bg='white', fg=INK,
             font=('Microsoft YaHei UI', 13, 'bold')).pack(anchor='w', pady=(0, 10))
    if description:
        label = tk.Label(card, text=description, bg='white', fg=MUTED, anchor='w', justify='left')
        label.pack(fill='x', pady=(0, 12))
        wrap_label(label)
    return card


class RoundedCard(tk.Frame):
    def __init__(self, parent, **kwargs):
        bg = parent.cget('bg') if 'bg' in parent.keys() else BG
        super().__init__(parent, bg=bg, bd=0, **kwargs)
        self.surface = tk.Canvas(self, bg=bg, bd=0, highlightthickness=0)
        self.surface.place(x=0, y=0, relwidth=1, relheight=1)
        self.content = tk.Frame(self, bg='white', padx=8, pady=6)
        self.content.pack(fill='both', expand=True, padx=12, pady=12)
        self.bind('<Configure>', self._draw, add='+')

    def _draw(self, event):
        if event.widget != self:
            return
        size = (event.width, event.height)
        if getattr(self, '_draw_size', None) == size:
            return
        self._draw_size = size
        w, h, r = event.width - 1, event.height - 1, 12
        self.surface.delete('all')
        points = [r, 1, w-r, 1, w, 1, w, r, w, h-r, w, h, w-r, h, r, h, 1, h, 1, h-r, 1, r, 1, 1]
        self.surface.create_polygon(points, smooth=True, splinesteps=24, fill='white', outline=BORDER, width=1)


class ScrollBar(tk.Canvas):
    """Rounded, focusable scrollbar using the standard yview/xview protocol."""
    def __init__(self, master, command=None, orient='vertical', **kwargs):
        self.vertical = orient == 'vertical'
        options = dict(bg='white', bd=0, highlightthickness=0, takefocus=1,
                       cursor='arrow', width=14 if self.vertical else 1,
                       height=1 if self.vertical else 14)
        options.update(kwargs)
        super().__init__(master, **options)
        self.command = command
        self.first, self.last = 0., 1.
        self.dragging, self.hover, self.focused = False, False, False
        self.bind('<Configure>', self._draw)
        self.bind('<Enter>', lambda e: self._state(hover=True))
        self.bind('<Leave>', lambda e: self._state(hover=False))
        self.bind('<FocusIn>', lambda e: self._state(focused=True))
        self.bind('<FocusOut>', lambda e: self._state(focused=False))
        self.bind('<ButtonPress-1>', self._press)
        self.bind('<B1-Motion>', self._drag)
        self.bind('<ButtonRelease-1>', lambda e: self._state(dragging=False))
        self.bind('<MouseWheel>', self._wheel)
        for key, amount, unit in [('Up', -1, 'units'), ('Down', 1, 'units'), ('Left', -1, 'units'),
                                  ('Right', 1, 'units'), ('Prior', -1, 'pages'), ('Next', 1, 'pages')]:
            self.bind('<' + key + '>', lambda e, n=amount, u=unit: self._send('scroll', n, u))
        self.bind('<Home>', lambda e: self._send('moveto', 0.))
        self.bind('<End>', lambda e: self._send('moveto', 1.))

    def set(self, first, last):
        self.first = max(0., min(1., float(first)))
        self.last = max(self.first, min(1., float(last)))
        self._draw()

    def _length(self):
        return max(1, (self.winfo_height() if self.vertical else self.winfo_width()) - 4)

    def thumb_bounds(self):
        length = self._length()
        size = min(length, max(28, length * (self.last - self.first)))
        remaining = 1 - (self.last - self.first)
        start = 2 + (length - size) * self.first / remaining if remaining > 0 else 2
        return start, start + size

    def _state(self, **values):
        for key, value in values.items():
            setattr(self, key, value)
        self._draw()

    def _draw(self, *_):
        if not hasattr(self, '_thumb'):
            self._thumb = self.create_line(0, 0, 0, 0, width=8, capstyle=tk.ROUND)
        if self.last - self.first >= .999999:
            self.itemconfigure(self._thumb, state='hidden')
            return
        start, end = self.thumb_bounds()
        color = BLUE if self.dragging or self.focused else ('#8D9BB0' if self.hover else '#C2CBD9')
        if self.vertical:
            x = self.winfo_width() / 2
            self.coords(self._thumb, x, start + 4, x, end - 4)
        else:
            y = self.winfo_height() / 2
            self.coords(self._thumb, start + 4, y, end - 4, y)
        self.itemconfigure(self._thumb, state='normal', fill=color)

    def _send(self, *args):
        if self.command and self.last - self.first < .999999:
            self.command(*args)
        return 'break'

    def _press(self, event):
        if self.last - self.first >= .999999:
            return
        self.focus_set()
        point = event.y if self.vertical else event.x
        start, end = self.thumb_bounds()
        if start <= point <= end:
            self.drag_origin = point
            self.drag_first = self.first
            self._state(dragging=True)
        else:
            self._send('scroll', -1 if point < start else 1, 'pages')

    def _drag(self, event):
        if not self.dragging:
            return
        start, end = self.thumb_bounds()
        travel = self._length() - (end - start)
        remaining = 1 - (self.last - self.first)
        if travel > 0:
            point = event.y if self.vertical else event.x
            value = self.drag_first + (point - self.drag_origin) / travel * remaining
            self._send('moveto', max(0., min(remaining, value)))

    def _wheel(self, event):
        target = getattr(self.command, '__self__', None)
        scale = 36 if isinstance(target, tk.Canvas) and float(target.cget('yscrollincrement')) == 1 else 1
        remainder = getattr(self, '_wheel_remainder', 0.0) - event.delta * scale / 120
        steps = int(remainder)
        self._wheel_remainder = remainder - steps
        if steps:
            self._send('scroll', steps, 'units')
        return 'break'


def bind_wheel(canvas):
    """Route wheel events from canvas descendants, without global bind_all hooks."""
    top = canvas.winfo_toplevel()
    canvas.configure(yscrollincrement=1)
    remainder = 0.0
    def wheel(event):
        nonlocal remainder
        widget = event.widget
        while widget is not None and widget != canvas:
            if isinstance(widget, (ttk.Treeview, tk.Text, ttk.Combobox, ttk.Spinbox)):
                return
            widget = getattr(widget, 'master', None)
        if widget == canvas and canvas.winfo_ismapped():
            first, last = canvas.yview()
            if last - first < .999999 and event.delta:
                remainder += -event.delta * 36 / 120
                pixels = int(remainder)
                remainder -= pixels
                if pixels:
                    canvas.yview_scroll(pixels, 'units')
                return 'break'
    binding = top.bind('<MouseWheel>', wheel, add='+')
    def cleanup(event):
        if event.widget == canvas:
            try:
                top.unbind('<MouseWheel>', binding)
            except tk.TclError:
                pass
    canvas.bind('<Destroy>', cleanup, add='+')
