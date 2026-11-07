// ReUI 桌面宿主：只承载同一库存进程提供的本机页面，不读取其他浏览器资料。
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;
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
    [STAThread]
    public static void Main(string[] args) {
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
