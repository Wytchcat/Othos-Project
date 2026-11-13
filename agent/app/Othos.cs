// Othos.exe: one taskbar button for Othos.
// Opening it starts Othos (start-othos.ps1) and opens the call page in a private
// window; the window shows what's running. Closing it (X, the Stop button, or
// right-click the taskbar button -> Close window) stops Othos (stop-othos.ps1).
// Build: build-othos-app.ps1 (uses the C# compiler that comes with Windows).
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net.Sockets;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

static class OthosApp
{
    internal static readonly string AgentDir = FindAgentDir();
    // The assistant's name and logo: personal.json's "assistant" section, as personal.py
    // reads it ("Othos" and the built-in icon when not set). Read once at start.
    internal static string Name = "Othos";
    internal static string Logo = "";

    static OthosApp() { ReadAssistant(); }

    static void ReadAssistant()
    {
        try
        {
            string json = File.ReadAllText(Path.Combine(AgentDir, "personal.json"));
            Match a = Regex.Match(json, "\"assistant\"\\s*:\\s*\\{([^}]*)\\}");
            if (!a.Success) return;
            string n = JsonText(a.Groups[1].Value, "name"), l = JsonText(a.Groups[1].Value, "logo");
            if (n.Length > 0) Name = n;
            if (l.Length > 0) Logo = Path.IsPathRooted(l) ? l : Path.Combine(AgentDir, l);
        }
        catch { }
    }

    static string JsonText(string obj, string key)
    {
        Match m = Regex.Match(obj, "\"" + key + "\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"");
        if (!m.Success) return "";
        try { return Regex.Unescape(m.Groups[1].Value).Trim(); } catch { return m.Groups[1].Value.Trim(); }
    }

    // The window icon from the logo: an .ico as it is, a PNG/JPG/BMP made into an icon
    // (an SVG can't be shown here, so the built-in icon stays).
    internal static Icon LogoIcon()
    {
        try
        {
            if (Logo.Length == 0 || !File.Exists(Logo)) return null;
            string ext = Path.GetExtension(Logo).ToLowerInvariant();
            if (ext == ".ico") return new Icon(Logo);
            if (ext == ".png" || ext == ".jpg" || ext == ".jpeg" || ext == ".bmp")
                using (var src = new Bitmap(Logo))
                using (var bmp = new Bitmap(src, new Size(32, 32)))
                    return Icon.FromHandle(bmp.GetHicon());
        }
        catch { }
        return null;
    }

    // The Othos-agent folder: the one holding agent.py, found from where this .exe
    // runs (app\ inside it), so the folder can live anywhere.
    static string FindAgentDir()
    {
        string d = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        for (int i = 0; i < 3 && d != null; i++)
        {
            if (File.Exists(Path.Combine(d, "agent.py"))) return d;
            d = Path.GetDirectoryName(d);
        }
        return Path.GetDirectoryName(AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\'));
    }
    const string Url = "http://127.0.0.1:3000";

    [STAThread]
    static void Main()
    {
        bool first;
        using (var mutex = new Mutex(true, @"Local\OthosApp", out first))
        {
            if (!first) { OpenPage(); return; }   // already open: just show the call page
            Application.EnableVisualStyles();
            Application.Run(new MainForm());
        }
    }

    public static void RunScript(string script, string args, bool wait, int waitMs)
    {
        var psi = new ProcessStartInfo("powershell.exe",
            "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"" +
            Path.Combine(AgentDir, script) + "\" " + args);
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        var p = Process.Start(psi);
        if (wait) p.WaitForExit(waitMs);
    }

    public static void OpenPage(string path = "")
    {
        var psi = new ProcessStartInfo("powershell.exe",
            "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command \". '" +
            Path.Combine(AgentDir, "open-private.ps1") + "'; Open-Private '" + Url + path + "'\"");
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        Process.Start(psi);
    }

    public static bool PortUp(int port)
    {
        try
        {
            using (var c = new TcpClient())
            {
                var r = c.BeginConnect("127.0.0.1", port, null, null);
                bool ok = r.AsyncWaitHandle.WaitOne(300) && c.Connected;
                if (ok) c.EndConnect(r);
                return ok;
            }
        }
        catch { return false; }
    }
}

class MainForm : Form
{
    // the Circuit Board's own colours: page #020705, chips and pads #050c08
    static readonly Color Bg = Color.FromArgb(2, 7, 5);
    static readonly Color Panel = Color.FromArgb(5, 12, 8);
    static readonly Color Edge = Color.FromArgb(60, 70, 66);
    // buttons: the board's purple trace colour (#9d7bff), blended into the background
    // (WinForms has no real transparency): fill ~8%, hover ~15%, outline ~40%
    static readonly Color PurpleFill = Color.FromArgb(14, 16, 25);
    static readonly Color PurpleHover = Color.FromArgb(25, 24, 43);
    static readonly Color PurpleLine = Color.FromArgb(64, 53, 105);
    static readonly Color Green = Color.FromArgb(61, 220, 132);
    static readonly Color Gold = Color.FromArgb(231, 195, 104);
    static readonly Color Purple = Color.FromArgb(157, 123, 255);   // the board's purple, for gaming mode
    static readonly Color Ink = Color.FromArgb(200, 230, 210);

    readonly Label title = new Label();
    readonly Label[] rows = new Label[4];
    readonly string[] names = { "LiveKit server", OthosApp.Name + " agent", "Call page", "Ollama (models)" };
    readonly int[] ports = { 7880, 8081, 3000, 11434 };
    readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
    readonly Label gamingRow = new Label();
    readonly Label speedRow = new Label();
    bool starting = true, stopping;

    // A reminder for the new build: run the llama.cpp worker speed test (WORKER.md,
    // "Speed test"). Shown only on a PC with 64 GB of RAM or more, where WORKER.md is
    // installed, until state\llamacpp-test-done.txt exists: create that file (any
    // content) to turn the row off. No pop-up.
    static readonly string SpeedDoneFile = Path.Combine(OthosApp.AgentDir, "state", "llamacpp-test-done.txt");

    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    class MemStatus
    {
        public uint dwLength = (uint)System.Runtime.InteropServices.Marshal.SizeOf(typeof(MemStatus));
        public uint dwMemoryLoad;
        public ulong ullTotalPhys, ullAvailPhys, ullTotalPageFile, ullAvailPageFile, ullTotalVirtual, ullAvailVirtual, ullAvailExtendedVirtual;
    }
    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GlobalMemoryStatusEx([System.Runtime.InteropServices.In, System.Runtime.InteropServices.Out] MemStatus m);

    static bool SpeedTestDue()
    {
        try
        {
            if (File.Exists(SpeedDoneFile) || !File.Exists(Path.Combine(OthosApp.AgentDir, "WORKER.md"))) return false;
            var m = new MemStatus();
            // 64 GB, less a little for memory the firmware and graphics keep back
            return GlobalMemoryStatusEx(m) && m.ullTotalPhys >= 60UL * 1024 * 1024 * 1024;
        }
        catch { return false; }
    }

    // Gaming mode (gaming.py): the button writes the same override file as the
    // "gaming mode on/off" voice command; the row shows what Othos last reported.
    static readonly string StateDir = Path.Combine(OthosApp.AgentDir, "state");
    static readonly string OverrideFile = Path.Combine(StateDir, "gaming-override.txt");
    static readonly string StatusFile = Path.Combine(StateDir, "gaming-status.json");

    static string ReadOverride()
    {
        try { return File.ReadAllText(OverrideFile).Trim().ToLowerInvariant(); } catch { return ""; }
    }

    static bool StatusOn(out string game, out string model)
    {
        game = ""; model = "";
        try
        {
            string s = File.ReadAllText(StatusFile);
            var g = System.Text.RegularExpressions.Regex.Match(s, "\"game\":\\s*\"([^\"]*)\"");
            var m = System.Text.RegularExpressions.Regex.Match(s, "\"voice_model\":\\s*\"([^\"]*)\"");
            game = g.Success ? g.Groups[1].Value : "";
            model = m.Success ? m.Groups[1].Value : "";
            return System.Text.RegularExpressions.Regex.IsMatch(s, "\"on\":\\s*true");
        }
        catch { return false; }
    }

    void ToggleGaming()
    {
        string game, model;
        string ov = ReadOverride();
        bool on = ov == "on" || (ov != "off" && StatusOn(out game, out model));
        try { Directory.CreateDirectory(StateDir); File.WriteAllText(OverrideFile, on ? "off" : "on"); } catch { }
    }

    public MainForm()
    {
        Text = OthosApp.Name;
        try { Icon = OthosApp.LogoIcon() ?? Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        bool speedDue = SpeedTestDue();
        int extra = speedDue ? 26 : 0;   // room for the reminder row
        btnY += extra;
        ClientSize = new Size(448, 256 + extra);
        BackColor = Bg;
        ForeColor = Ink;
        Font = new Font("Segoe UI", 10f);

        title.Text = string.Join(" ", OthosApp.Name.ToUpperInvariant().ToCharArray());
        title.Font = new Font("Segoe UI Semibold", 14f);
        title.ForeColor = Green;
        title.SetBounds(20, 14, 300, 30);
        Controls.Add(title);

        for (int i = 0; i < rows.Length; i++)
        {
            rows[i] = new Label();
            rows[i].SetBounds(22, 52 + i * 26, 300, 24);
            Controls.Add(rows[i]);
        }

        gamingRow.SetBounds(22, 52 + 4 * 26, 410, 24);
        Controls.Add(gamingRow);

        if (speedDue)
        {
            speedRow.SetBounds(22, 52 + 5 * 26, 410, 24);
            speedRow.ForeColor = Gold;
            speedRow.Text = "◆  New build: run the llama.cpp worker speed test (WORKER.md)";
            Controls.Add(speedRow);
        }

        // Code chat moved to the call screen's control bar (the </> button) and to The Smithy's window
        var open = MakeButton("Call page", 20);
        open.Click += (s, e) => OthosApp.OpenPage();
        var game = MakeButton("Gaming", 159);
        game.Click += (s, e) => { ToggleGaming(); Refresh_(); };
        var stop = MakeButton("Stop " + OthosApp.Name, 298);
        stop.Click += (s, e) => Close();

        timer.Interval = 3000;
        timer.Tick += (s, e) => Refresh_();
        Refresh_();
    }

    int btnY = 200;

    Button MakeButton(string text, int x)
    {
        var b = new Button();
        b.Text = text;
        b.SetBounds(x, btnY, 130, 36);
        b.FlatStyle = FlatStyle.Flat;
        b.FlatAppearance.BorderColor = PurpleLine;
        b.FlatAppearance.MouseOverBackColor = PurpleHover;
        b.FlatAppearance.MouseDownBackColor = PurpleHover;
        b.BackColor = PurpleFill;
        b.ForeColor = Ink;
        Controls.Add(b);
        return b;
    }

    protected override void OnShown(EventArgs e)
    {
        base.OnShown(e);
        WindowState = FormWindowState.Minimized;   // lives on the taskbar
        // start-othos.ps1 waits for the call page (up to ~2 minutes), so run it off the UI thread
        ThreadPool.QueueUserWorkItem(_ =>
        {
            OthosApp.RunScript("start-othos.ps1", "", true, 180000);
            starting = false;
            try { BeginInvoke((Action)Refresh_); } catch { }
        });
        timer.Start();
    }

    void Refresh_()
    {
        string sgame, smodel;
        bool son = StatusOn(out sgame, out smodel);
        string ov = ReadOverride();
        string gtxt = son ? "Gaming mode on" + (sgame != "" ? " (" + sgame + ")" : "") + (smodel != "" ? ", " + smodel : "")
                          : "Gaming mode off";
        if (ov == "on" && !son) gtxt = "Gaming mode on: starts with the next call";
        if (ov == "off" && son) gtxt = "Gaming mode off: applies within 10 s of a call";
        gamingRow.ForeColor = son || ov == "on" ? Purple : Ink;
        gamingRow.Text = "◆  " + gtxt;
        if (speedRow.Visible && File.Exists(SpeedDoneFile)) speedRow.Visible = false;   // turned off while open
        bool all = true;
        for (int i = 0; i < rows.Length; i++)
        {
            bool up = OthosApp.PortUp(ports[i]);
            all &= up;
            rows[i].ForeColor = up ? Green : Gold;
            rows[i].Text = (up ? "\u25CF  " : "\u25CB  ") + names[i] + (up ? "  running" : (starting ? "  starting\u2026" : "  not running"));
        }
        string n = OthosApp.Name;
        Text = stopping ? n + ": stopping\u2026" : all ? n + ": running" : (starting ? n + ": starting\u2026" : n + ": part not running");
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        if (!stopping)
        {
            stopping = true;
            timer.Stop();
            Text = OthosApp.Name + ": stopping\u2026";
            Cursor = Cursors.WaitCursor;
            Application.DoEvents();
            OthosApp.RunScript("stop-othos.ps1", "-Quiet", true, 20000);
        }
        base.OnFormClosing(e);
    }
}
