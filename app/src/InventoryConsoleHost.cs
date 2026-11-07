// 原生业务页作为宿主内容区的子窗口；只接收主程序创建的窗口句柄。
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Web.Script.Serialization;
using System.Collections.Generic;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

public sealed class NativeRegion : IDisposable {
    [DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr window);
    [DllImport("user32.dll")] private static extern IntPtr SetParent(IntPtr window, IntPtr parent);
    [DllImport("user32.dll")] private static extern int GetWindowLong(IntPtr window,int index);
    [DllImport("user32.dll")] private static extern int SetWindowLong(IntPtr window,int index,int value);
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr window,int command);
    [DllImport("user32.dll")] private static extern bool SetWindowPos(IntPtr window,IntPtr after,int x,int y,int width,int height,uint flags);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window,out uint pid);
    [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr window);
    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [StructLayout(LayoutKind.Sequential)] public struct Rect {public int Left,Top,Right,Bottom;}
    [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr window,out Rect rect);
    [DllImport("user32.dll")] private static extern IntPtr GetWindow(IntPtr window,uint command);
    private IntPtr window, originalParent;
    private int style;
    public NativeRegion(IntPtr child,IntPtr parent,int expectedProcess) {
        if(child==IntPtr.Zero)return;
        uint pid;GetWindowThreadProcessId(child,out pid);
        if(!IsWindow(child)||pid!=expectedProcess)throw new ArgumentException("业务窗口不属于主程序");
        window=child;originalParent=GetParent(child);style=GetWindowLong(child,-16);
        ShowWindow(child,0);
        // SetParent 不会调整窗口样式，显式去除标题栏和弹出窗口属性。
        SetWindowLong(child,-16,(style & ~unchecked((int)0x80C40000))|0x40000000);
        SetParent(child,parent);
        if(GetParent(child)!=parent){Dispose();throw new InvalidOperationException("业务页嵌入失败");}
    }
    public void Layout(bool visible,int x,int y,int width,int height) {
        if(window==IntPtr.Zero||!IsWindow(window))return;
        if(!visible||width<1||height<1){ShowWindow(window,0);return;}
        SetWindowPos(window,IntPtr.Zero,x,y,width,height,0x0010|0x0020|0x0040);
        // Tk 的外层包装窗在嵌入后不总是转发 WM_SIZE，显式同步其客户控件。
        IntPtr content=GetWindow(window,5);
        if(content!=IntPtr.Zero)SetWindowPos(content,IntPtr.Zero,0,0,width,height,0x0010|0x0004|0x0040);
    }
    public void Dispose() {
        if(window==IntPtr.Zero)return;
        if(IsWindow(window)){
            // 先隐藏并脱离宿主，再销毁主窗口，保留托盘服务和业务控件供下次打开。
            ShowWindow(window,0);SetParent(window,originalParent);SetWindowLong(window,-16,style);
            SetWindowPos(window,IntPtr.Zero,-20000,-20000,1080,780,0x0010|0x0020|0x0004);
        }
        window=IntPtr.Zero;
    }
}

public sealed class InventoryConsoleHost : Form {
    private readonly WebView2 view = new WebView2();
    private readonly Uri address;
    private readonly int parentId;
    private readonly Timer lifetime = new Timer();
    private NativeRegion native;
    public InventoryConsoleHost(Uri url,int parent,IntPtr child) {
        address=url;parentId=parent;
        Text="后台库存监控";Width=1440;Height=960;
        MinimumSize=new Size(760,560);StartPosition=FormStartPosition.CenterScreen;
        // EXE、任务栏和窗口统一使用原软件图标，不使用默认 WinForms 图标。
        Icon=Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        view.Dock=DockStyle.Fill;Controls.Add(view);
        native=new NativeRegion(child,Handle,parent);
        Shown+=Initialize;
        lifetime.Interval=1000;
        lifetime.Tick+=delegate {
            try {if(Process.GetProcessById(parentId).HasExited)Close();}
            catch(ArgumentException){Close();}
        };
        lifetime.Start();
        FormClosing+=delegate {native.Dispose();};
        FormClosed+=delegate {lifetime.Dispose();view.Dispose();};
    }
    private void RegionMessage(object sender,CoreWebView2WebMessageReceivedEventArgs e) {
        Uri source;
        if(!Uri.TryCreate(e.Source,UriKind.Absolute,out source)||source.GetLeftPart(UriPartial.Authority)!=address.GetLeftPart(UriPartial.Authority))return;
        try {
            var data=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(e.WebMessageAsJson);
            if(!data.ContainsKey("type")||Convert.ToString(data["type"])!="native-region")return;
            if(!Convert.ToBoolean(data["visible"])) {native.Layout(false,0,0,0,0);return;}
            // CSS 像素按实际 WebView 客户区换算，适配缩放和不同 DPI 显示器。
            double vw=Convert.ToDouble(data["viewportWidth"]),vh=Convert.ToDouble(data["viewportHeight"]);
            if(vw<=0||vh<=0)return;
            double sx=view.ClientSize.Width/vw,sy=view.ClientSize.Height/vh;
            int x=Math.Max(0,(int)Math.Round(Convert.ToDouble(data["x"])*sx));
            int y=Math.Max(0,(int)Math.Round(Convert.ToDouble(data["y"])*sy));
            int width=Math.Min(view.ClientSize.Width-x,(int)Math.Round(Convert.ToDouble(data["width"])*sx));
            int height=Math.Min(view.ClientSize.Height-y,(int)Math.Round(Convert.ToDouble(data["height"])*sy));
            native.Layout(true,x,y,width,height);
        } catch(Exception) {native.Layout(false,0,0,0,0);}
    }
    private async void Initialize(object sender,EventArgs args) {
        try {
            string profile=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"InventoryMonitor","ConsoleWebView2");
            var environment=await CoreWebView2Environment.CreateAsync(null,profile);
            await view.EnsureCoreWebView2Async(environment);
            view.CoreWebView2.Settings.AreDefaultContextMenusEnabled=false;
            view.CoreWebView2.Settings.AreDevToolsEnabled=false;
            view.CoreWebView2.Settings.IsStatusBarEnabled=false;
            view.CoreWebView2.WebMessageReceived+=RegionMessage;
            view.CoreWebView2.NewWindowRequested+=delegate(object s,CoreWebView2NewWindowRequestedEventArgs e){e.Handled=true;};
            view.CoreWebView2.NavigationStarting+=delegate(object s,CoreWebView2NavigationStartingEventArgs e){
                native.Layout(false,0,0,0,0);
                Uri destination;
                if(!Uri.TryCreate(e.Uri,UriKind.Absolute,out destination)||destination.GetLeftPart(UriPartial.Authority)!=address.GetLeftPart(UriPartial.Authority))e.Cancel=true;
            };
            view.Source=address;
        } catch(Exception) {
            MessageBox.Show("工作台窗口未能启动，请检查 Microsoft Edge WebView2 Runtime。可使用 --legacy-ui 启动原生管理界面。","工作台启动失败",MessageBoxButtons.OK,MessageBoxIcon.Error);
            Close();
        }
    }
    [DllImport("shell32.dll", CharSet=CharSet.Unicode)] private static extern int SetCurrentProcessExplicitAppUserModelID(string appId);
    [STAThread]
    public static void Main(string[] args) {
        SetCurrentProcessExplicitAppUserModelID("Local.BackendInventoryMonitor");
        NativeRegion.SetProcessDpiAwarenessContext(new IntPtr(-4));
        if(args.Length==2&&args[0]=="--self-check"){
            try {File.WriteAllText(args[1],CoreWebView2Environment.GetAvailableBrowserVersionString());}
            catch(Exception){Environment.ExitCode=1;}
            return;
        }
        if(args.Length==4&&args[0]=="--embed-check"){
            // 不显示桌面窗口，只在隐藏宿主验证跨进程父子关系与脱离生命周期。
            try {
                IntPtr child=new IntPtr(Int64.Parse(args[1]));int owner=Int32.Parse(args[2]);
                IntPtr original=NativeRegion.GetParent(child);
                using(var form=new Form()){
                    form.ShowInTaskbar=false;form.Opacity=0;form.StartPosition=FormStartPosition.Manual;form.Location=new Point(-20000,-20000);
                    form.Show();
                    using(var region=new NativeRegion(child,form.Handle,owner)){
                    region.Layout(true,30,40,600,400);
                    if(NativeRegion.GetParent(child)!=form.Handle)throw new Exception("嵌入失败");
                    NativeRegion.Rect bounds;NativeRegion.GetClientRect(child,out bounds);
                    if(bounds.Right!=600||bounds.Bottom!=400)throw new Exception("内容区尺寸错误");
                    System.Threading.Thread.Sleep(300);
                    region.Layout(false,0,0,0,0);
                    region.Dispose();
                    if(NativeRegion.GetParent(child)!=original)throw new Exception("脱离失败");
                    File.WriteAllText(args[3],"embedded-resized-hidden-detached");
                    }
                }
            } catch(Exception){Environment.ExitCode=1;}
            return;
        }
        Uri url;int parent;long childId;
        if(args.Length!=3||!Uri.TryCreate(args[0],UriKind.Absolute,out url)||url.Scheme!="http"||url.Host!="127.0.0.1"||!Int32.TryParse(args[1],out parent)||!Int64.TryParse(args[2],out childId))return;
        Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new InventoryConsoleHost(url,parent,new IntPtr(childId)));
    }
}
