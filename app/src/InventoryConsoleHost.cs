// ReUI 桌面宿主：只承载同一库存进程提供的本机页面，不读取其他浏览器资料。
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;
using System.Runtime.InteropServices;
using System.Web.Script.Serialization;
using System.Collections.Generic;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

public sealed class InventoryConsoleHost : Form {
    private readonly WebView2 view = new WebView2();
    private readonly Uri address;
    private readonly int parentId;
    private readonly Timer lifetime = new Timer();
    public InventoryConsoleHost(Uri url, int parent) {
        address = url; parentId = parent;
        Text = "后台库存监控";
        Icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        Width = 1440; Height = 960;
        MinimumSize = new System.Drawing.Size(760, 560);
        StartPosition = FormStartPosition.CenterScreen;
        view.Dock = DockStyle.Fill;
        Controls.Add(view);
        Shown += Initialize;
        // 主程序完全退出后关闭宿主，关闭此窗口本身则保留主程序的托盘后台。
        lifetime.Interval = 1000;
        lifetime.Tick += delegate {
            try { if (Process.GetProcessById(parentId).HasExited) Close(); }
            catch (ArgumentException) { Close(); }
        };
        lifetime.Start();
        FormClosed += delegate { lifetime.Dispose(); view.Dispose(); };
    }
    private async void Initialize(object sender, EventArgs args) {
        try {
            string profile = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "InventoryMonitor", "ConsoleWebView2");
            var environment = await CoreWebView2Environment.CreateAsync(null, profile);
            await view.EnsureCoreWebView2Async(environment);
            view.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
            view.CoreWebView2.Settings.AreDevToolsEnabled = false;
            view.CoreWebView2.Settings.IsStatusBarEnabled = false;
            // 文件选择仅在用户点击时执行，业务页面不再嵌入任何原生子窗口。
            view.CoreWebView2.WebMessageReceived += delegate(object s, CoreWebView2WebMessageReceivedEventArgs e) {
                Uri source;
                if (!Uri.TryCreate(e.Source,UriKind.Absolute,out source) || source.GetLeftPart(UriPartial.Authority)!=address.GetLeftPart(UriPartial.Authority)) return;
                try {
                    var value=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(e.WebMessageAsJson);
                    if(Convert.ToString(value["type"])=="choose-file") {
                        using(var dialog=new OpenFileDialog()) {
                            dialog.Filter="业务文件|*.json;*.xlsx;*.csv;*.xlsb;*.xlsm";
                            var path=dialog.ShowDialog(this)==DialogResult.OK?dialog.FileName:"";
                            view.CoreWebView2.PostWebMessageAsJson(new JavaScriptSerializer().Serialize(new {type="chosen-file",id=Convert.ToString(value["id"]),path=path}));
                        }
                    } else if(Convert.ToString(value["type"])=="open-external") {
                        Uri url;
                        if(Uri.TryCreate(Convert.ToString(value["url"]),UriKind.Absolute,out url)&&url.Scheme=="https"&&String.IsNullOrEmpty(url.UserInfo))
                            Process.Start(new ProcessStartInfo(url.AbsoluteUri){UseShellExecute=true});
                    }
                } catch(Exception) { /* 消息格式不合法时不执行系统操作。 */ }
            };
            view.CoreWebView2.NewWindowRequested += delegate(object s, CoreWebView2NewWindowRequestedEventArgs e) { e.Handled = true; };
            view.CoreWebView2.NavigationStarting += delegate(object s, CoreWebView2NavigationStartingEventArgs e) {
                Uri destination;
                if (!Uri.TryCreate(e.Uri, UriKind.Absolute, out destination) || destination.GetLeftPart(UriPartial.Authority) != address.GetLeftPart(UriPartial.Authority)) e.Cancel = true;
            };
            view.Source = address;
        } catch (Exception) {
            MessageBox.Show("工作台窗口未能启动，请检查 Microsoft Edge WebView2 Runtime。可使用 --legacy-ui 启动原生管理界面。", "工作台启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
            Close();
        }
    }
    [DllImport("shell32.dll",CharSet=CharSet.Unicode)] private static extern int SetCurrentProcessExplicitAppUserModelID(string id);
    [DllImport("user32.dll")] private static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [STAThread]
    public static void Main(string[] args) {
        SetCurrentProcessExplicitAppUserModelID("Local.BackendInventoryMonitor");
        SetProcessDpiAwarenessContext(new IntPtr(-4));
        if (args.Length == 2 && args[0] == "--self-check") {
            // 候选包验收只加载运行库，不启动窗口或用户浏览器。
            try { File.WriteAllText(args[1], CoreWebView2Environment.GetAvailableBrowserVersionString()); }
            catch (Exception) { Environment.ExitCode = 1; }
            return;
        }
        Uri url; int parent;
        // 参数来自主程序，不接受外部站点作为桌面内容。
        if (args.Length != 2 || !Uri.TryCreate(args[0], UriKind.Absolute, out url) || url.Scheme != "http" || url.Host != "127.0.0.1" || !Int32.TryParse(args[1], out parent)) return;
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new InventoryConsoleHost(url, parent));
    }
}
