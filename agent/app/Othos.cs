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

    // A web page in a private window (the model check's Open page button): https on ollama.com only.
    public static void OpenUrl(string url)
    {
        if (!Regex.IsMatch(url, "^https://ollama\\.com/[A-Za-z0-9/_.-]*$")) return;
        var psi = new ProcessStartInfo("powershell.exe",
            "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command \". '" +
            Path.Combine(AgentDir, "open-private.ps1") + "'; Open-Private '" + url + "'\"");
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        Process.Start(psi);
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
    readonly Label modelsRow = new Label();
    Button[] buttons = new Button[0];
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
        StartPosition = FormStartPosition.CenterScreen;
        bool speedDue = SpeedTestDue();
        int extra = speedDue ? 26 : 0;   // room for the reminder row
        btnY += extra;
        ClientSize = new Size(448, 256 + extra);
        WindowFit.Apply(this, new Size(448, 256 + extra));   // sizable; the contents below are anchored
        BackColor = Bg;
        ForeColor = Ink;
        Font = new Font("Segoe UI", 10f);

        title.Text = string.Join(" ", OthosApp.Name.ToUpperInvariant().ToCharArray());
        title.Font = new Font("Segoe UI Semibold", 14f);
        title.ForeColor = Green;
        title.SetBounds(20, 14, 190, 30);
        Controls.Add(title);

        // the model check (model_check.py): weekly Yes/No, or any time from here
        var models = new LinkLabel();
        models.Text = "Check models...";
        models.LinkColor = Gold;
        models.ActiveLinkColor = Green;
        models.SetBounds(212, 22, 112, 22);
        models.Anchor = AnchorStyles.Top | AnchorStyles.Right;
        models.TextAlign = ContentAlignment.MiddleRight;
        models.LinkClicked += (s, e) => ShowModels();
        Controls.Add(models);

        var check = new LinkLabel();
        check.Text = "Check my PC...";
        check.LinkColor = Gold;
        check.ActiveLinkColor = Green;
        check.SetBounds(330, 22, 104, 22);
        check.Anchor = AnchorStyles.Top | AnchorStyles.Right;
        check.TextAlign = ContentAlignment.MiddleRight;
        check.LinkClicked += (s, e) => ShowSpecs();
        Controls.Add(check);

        for (int i = 0; i < rows.Length; i++)
        {
            rows[i] = new Label();
            rows[i].SetBounds(22, 52 + i * 26, 300, 24);
            Controls.Add(rows[i]);
        }

        gamingRow.SetBounds(22, 52 + 4 * 26, 410, 24);
        gamingRow.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
        Controls.Add(gamingRow);

        if (speedDue)
        {
            speedRow.SetBounds(22, 52 + 5 * 26, 410, 24);
            speedRow.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            speedRow.ForeColor = Gold;
            speedRow.Text = "◆  New build: run the llama.cpp worker speed test (WORKER.md)";
            Controls.Add(speedRow);
        }

        // Code chat moved to the call screen's control bar (the </> button) and to The Smithy's window
        // the automatic model check's line (shown only when it found something new)
        modelsRow.SetBounds(22, 52 + (speedDue ? 6 : 5) * 26, 410, 24);
        modelsRow.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
        modelsRow.ForeColor = Gold;
        modelsRow.Cursor = Cursors.Hand;
        modelsRow.Visible = false;
        modelsRow.Click += (s, e) => { modelsRow.Visible = false; ShowModels(); };
        Controls.Add(modelsRow);

        var open = MakeButton("Call page", 20);
        open.Click += (s, e) => OthosApp.OpenPage();
        var game = MakeButton("Gaming", 159);
        game.Click += (s, e) => { ToggleGaming(); Refresh_(); };
        var stop = MakeButton("Stop " + OthosApp.Name, 298);
        stop.Click += (s, e) => Close();
        buttons = new[] { open, game, stop };

        timer.Interval = 3000;
        timer.Tick += (s, e) => Refresh_();
        Refresh_();
    }


    // ---- this PC's specs (system_specs.py): asked once on first start, "Check my PC..." any time
    static readonly string SpecsFile = Path.Combine(OthosApp.AgentDir, "state", "system-specs.json");

    static string RunSpecs(string args) { return RunPy("system_specs.py", args); }

    static string RunPy(string script, string args)
    {
        try
        {
            var psi = new ProcessStartInfo(Path.Combine(OthosApp.AgentDir, ".venv", "Scripts", "python.exe"),
                "\"" + Path.Combine(OthosApp.AgentDir, script) + "\" " + args);
            psi.WorkingDirectory = OthosApp.AgentDir;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            psi.StandardOutputEncoding = System.Text.Encoding.UTF8;
            psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            using (var pr = Process.Start(psi))
            {
                string o = pr.StandardOutput.ReadToEnd();
                string err = pr.StandardError.ReadToEnd();
                pr.WaitForExit(120000);
                return pr.ExitCode == 0 ? o.Trim() : (o + "\r\n" + err).Trim();
            }
        }
        catch (Exception ex) { return "Couldn't run " + script + ": " + ex.Message; }
    }

    // A Yes/No question in the Othos window's own look (not Windows' grey message box).
    internal bool AskYesNo(IWin32Window owner, string heading, string text, string yes, string no, string caption = "this PC")
    {
        using (var d = new Form())
        {
            d.Text = OthosApp.Name + ": " + caption;
            d.Icon = Icon;
            d.StartPosition = owner == null ? FormStartPosition.CenterScreen : FormStartPosition.CenterParent;
            d.ShowInTaskbar = owner == null;
            d.BackColor = Bg;
            d.ForeColor = Ink;
            d.Font = new Font("Segoe UI", 10f);
            // the height the text needs at the opening width, then the window's final size, BEFORE the
            // controls are added: Anchor remembers each control's distance to the edges as it is added
            var t = new Label();
            t.Text = text;
            t.Font = d.Font;
            t.ForeColor = Ink;
            t.AutoSize = true;
            t.MaximumSize = new Size(440, 0);
            int th = t.PreferredHeight;
            int by = 54 + th + 20;
            d.ClientSize = new Size(484, by + 56);
            var h = new Label();
            h.Text = heading;
            h.Font = new Font("Segoe UI Semibold", 14f);
            h.ForeColor = Green;
            h.SetBounds(20, 14, 440, 30);
            h.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            d.Controls.Add(h);
            t.MaximumSize = Size.Empty;
            t.AutoSize = false;   // follows the window's size (wraps again when it's resized)
            t.SetBounds(22, 54, 440, th);
            t.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Bottom;
            d.Controls.Add(t);
            var y = new Button();
            var n = new Button();
            y.Text = yes; n.Text = no;
            y.DialogResult = DialogResult.Yes; n.DialogResult = DialogResult.No;
            int x = 22;
            foreach (var b in new[] { y, n })
            {
                b.SetBounds(x, by, 150, 36);
                b.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
                b.FlatStyle = FlatStyle.Flat;
                b.FlatAppearance.BorderColor = b == y ? Green : PurpleLine;
                b.FlatAppearance.MouseOverBackColor = PurpleHover;
                b.FlatAppearance.MouseDownBackColor = PurpleHover;
                b.BackColor = PurpleFill;
                b.ForeColor = Ink;
                d.Controls.Add(b);
                x += 160;
            }
            d.AcceptButton = y;
            d.CancelButton = n;
            WindowFit.ApplyDialog(d, d.ClientSize);   // sizable; no smaller than the text and buttons need
            return (owner == null ? d.ShowDialog() : d.ShowDialog(owner)) == DialogResult.Yes;
        }
    }

    void AskSpecsOnce()
    {
        if (File.Exists(SpecsFile) || !File.Exists(Path.Combine(OthosApp.AgentDir, "system_specs.py"))) return;
        bool yes = AskYesNo(this, "T H I S   P C",
            "May I check your current system specs, so I can best advise you of providers and set up?\n\n" +
            "This reads your PC's hardware only. Nothing leaves this computer.",
            "Yes, check my PC", "No thanks");
        if (yes) ShowSpecs();
        else ThreadPool.QueueUserWorkItem(_ => RunSpecs("--decline"));
    }

    static readonly Color Dim = Color.FromArgb(140, 162, 150);
    static readonly Color Red = Color.FromArgb(232, 110, 110);

    // The advice window, in the Othos window's own look: a heading, then one row per piece
    // of advice with a coloured tag (Fits / Tight / Won't fit), then Apply and Close.
    void ShowSpecs()
    {
        var f = new Form();
        f.Text = OthosApp.Name + ": this PC";
        f.Icon = Icon;
        f.StartPosition = FormStartPosition.CenterScreen;
        f.ClientSize = new Size(720, 540);
        f.BackColor = Bg;
        f.ForeColor = Ink;
        f.Font = new Font("Segoe UI", 10f);

        var head = new Label();
        head.Text = "T H I S   P C";
        head.Font = new Font("Segoe UI Semibold", 14f);
        head.ForeColor = Green;
        head.SetBounds(20, 14, 400, 30);
        f.Controls.Add(head);
        var sub = new Label();
        sub.ForeColor = Dim;
        sub.SetBounds(22, 46, 680, 40);
        sub.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
        sub.Text = "Reading this PC's specs (about 10 seconds). Nothing leaves this computer.";
        f.Controls.Add(sub);

        var list = new System.Windows.Forms.Panel();
        list.AutoScroll = true;
        list.BackColor = Panel;
        list.BorderStyle = BorderStyle.FixedSingle;
        list.SetBounds(20, 92, 680, 376);
        list.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
        f.Controls.Add(list);

        var apply = new Button();
        apply.Text = "Apply these settings";
        apply.Enabled = false;
        var close = new Button();
        close.Text = "Close";
        int x = 20;
        foreach (var b in new[] { apply, close })
        {
            b.SetBounds(x, 484, b == apply ? 190 : 110, 36);
            b.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
            b.FlatStyle = FlatStyle.Flat;
            b.FlatAppearance.BorderColor = PurpleLine;
            b.FlatAppearance.MouseOverBackColor = PurpleHover;
            b.BackColor = PurpleFill;
            b.ForeColor = Ink;
            f.Controls.Add(b);
            x += b.Width + 10;
        }
        close.Click += (s, e) => f.Close();

        // rows: {fit, area, text}; fit "" = a plain line (the apply results)
        string[][] lastRows = null;   // kept so the rows can be laid out again when the window is resized
        int lastW = 0;
        Action<string[][]> fill = rows =>
        {
            lastRows = rows;
            lastW = list.Width;
            list.SuspendLayout();
            list.Controls.Clear();
            int y = 10, textW = Math.Max(160, list.Width - 270);
            foreach (var r in rows)
            {
                string fit = r[0], area = r[1], text = r[2];
                int h = 24;
                if (fit.Length > 0)
                {
                    var tag = new Label();
                    tag.Text = fit == "fits" ? "Fits" : fit == "tight" ? "Tight" : "Won't fit";
                    tag.ForeColor = fit == "fits" ? Green : fit == "tight" ? Gold : Red;
                    tag.BackColor = Bg;
                    tag.TextAlign = ContentAlignment.MiddleCenter;
                    tag.Font = new Font("Segoe UI Semibold", 9f);
                    tag.SetBounds(10, y, 74, 22);
                    list.Controls.Add(tag);
                }
                var a = new Label();
                a.Text = area;
                a.ForeColor = Ink;
                a.Font = new Font("Segoe UI Semibold", 10f);
                a.SetBounds(fit.Length > 0 ? 94 : 10, y + 1, 130, 22);
                list.Controls.Add(a);
                var t = new Label();
                t.Text = text;
                t.ForeColor = Ink;
                t.AutoSize = true;
                t.MaximumSize = new Size(textW, 0);
                t.Location = new Point(230, y + 1);
                list.Controls.Add(t);
                h = Math.Max(h, t.PreferredHeight);
                y += h + 10;
            }
            list.ResumeLayout();
        };
        list.Resize += (s, e) => { if (lastRows != null && list.Width != lastW) fill(lastRows); };
        Func<string, string[][]> parse = output =>
        {
            var rows = new System.Collections.Generic.List<string[]>();
            foreach (var line in output.Replace("\r", "").Split('\n'))
            {
                var c = line.Split('\t');
                if (c.Length == 3) rows.Add(c);
                else if (line.Trim().Length > 0) rows.Add(new[] { "", "", line.Trim() });
            }
            return rows.ToArray();
        };

        apply.Click += (s, e) =>
        {
            if (!AskYesNo(f, "A P P L Y",
                    "Apply these settings? Your settings files are backed up first (in state\\backups), " +
                    "and only the settings listed are changed.", "Yes, apply", "Not now")) return;
            apply.Enabled = false;
            sub.Text = "Applying...";
            ThreadPool.QueueUserWorkItem(_ =>
            {
                string o = RunSpecs("--apply");
                try
                {
                    f.BeginInvoke((Action)(() =>
                    {
                        sub.Text = "Done. Backups are in state\\backups.";
                        var rows = new System.Collections.Generic.List<string[]>();
                        foreach (var line in o.Replace("\r", "").Split('\n'))
                        {
                            if (line.Trim().Length == 0) continue;
                            int colon = line.IndexOf(": ");
                            rows.Add(colon > 0 && colon < 24
                                ? new[] { "", line.Substring(0, colon), line.Substring(colon + 2) }
                                : new[] { "", "", line.Trim() });
                        }
                        fill(rows.ToArray());
                    }));
                }
                catch { }
            });
        };
        f.Shown += (s, e) => ThreadPool.QueueUserWorkItem(_ =>
        {
            string o = RunSpecs("--read --rows");
            try
            {
                f.BeginInvoke((Action)(() =>
                {
                    var rows = parse(o);
                    var rest = new System.Collections.Generic.List<string[]>();
                    string pc = "";
                    foreach (var r in rows) { if (r[1] == "PC" && pc.Length == 0) pc = r[2]; else rest.Add(r); }
                    bool ok = rest.Count > 0 && rest[0][0].Length > 0;
                    sub.Text = ok ? pc + "\nRead on this computer only. Saved in state\\system-specs.json."
                                  : "Couldn't read this PC's specs.";
                    fill(rest.ToArray());
                    apply.Enabled = ok;
                }));
            }
            catch { }
        });
        WindowFit.Apply(f, new Size(560, 380));   // sizable, with a minimum; opens no bigger than the screen's free area
        f.Show();   // not owned: the Othos window minimizes to the taskbar
    }

    // ---- the weekly model check (model_check.py). It contacts ollama.com, Hugging Face and
    // GitHub (model names only), so it runs only on a Yes, from "Check models...", or with
    // "model_check": "auto" in personal.json. It never downloads or changes anything.
    void ModelCheckIfDue()
    {
        if (!File.Exists(Path.Combine(OthosApp.AgentDir, "model_check.py"))) return;
        ThreadPool.QueueUserWorkItem(_ =>
        {
            string due = RunPy("model_check.py", "--due").Trim();
            if (due == "ask")
            {
                try { BeginInvoke((Action)AskModels); } catch { }
            }
            else if (due == "auto")
            {
                RunPy("model_check.py", "--rows");
                int news = ModelNews();
                if (news > 0) try { BeginInvoke((Action)(() => ShowModelsRow(news))); } catch { }
            }
        });
    }

    void AskModels()
    {
        // the window lives minimized on the taskbar, so the question stands on its own
        bool min = WindowState == FormWindowState.Minimized;
        bool yes = AskYesNo(min ? null : this, "M O D E L S",
            "Check for updated models?\n\n" +
            "This looks for new or updated models for " + OthosApp.Name + " and The Smithy, and whether this PC can run them. " +
            "It asks ollama.com, Hugging Face and GitHub, sending only model names. It also re-checks the free limits of the " +
            "model providers you turned on in The Smithy (their own account pages, with your saved keys). Nothing is downloaded.",
            "Yes, check", "Not now", "models");
        if (yes) ShowModels();
        else ThreadPool.QueueUserWorkItem(_ => RunPy("model_check.py", "--snooze"));
    }

    static int ModelNews()
    {
        try
        {
            var m = Regex.Match(File.ReadAllText(Path.Combine(OthosApp.AgentDir, "state", "model-check.json")), "\"news\":\\s*(\\d+)");
            return m.Success ? int.Parse(m.Groups[1].Value) : 0;
        }
        catch { return 0; }
    }

    void ShowModelsRow(int news)
    {
        if (modelsRow.Visible) return;
        modelsRow.Text = "◆  Model check: " + news + " new or updated (click to see)";
        modelsRow.Visible = true;
        // make room for the new row: a bigger minimum grows the window if it's at its smallest, and
        // the buttons (anchored to the bottom) move down with it
        MinimumSize = new Size(MinimumSize.Width, MinimumSize.Height + 26);
    }

    // The model check's window, in the same look as the PC check: a heading, then the rows
    // grouped by job (voice, camera, notes, coding, Ollama, providers) with a coloured tag.
    // A model this PC can run gets a Get (or Update) button: a Yes/No with its size and the
    // free space first, then progress at the top and a Cancel button. One download at a time.
    void ShowModels()
    {
        var f = new Form();
        f.Text = OthosApp.Name + ": models";
        f.Icon = Icon;
        f.StartPosition = FormStartPosition.CenterScreen;
        f.ClientSize = new Size(760, 580);
        f.BackColor = Bg;
        f.ForeColor = Ink;
        f.Font = new Font("Segoe UI", 10f);

        var head = new Label();
        head.Text = "M O D E L S";
        head.Font = new Font("Segoe UI Semibold", 14f);
        head.ForeColor = Green;
        head.SetBounds(20, 14, 400, 30);
        f.Controls.Add(head);
        var sub = new Label();
        sub.ForeColor = Dim;
        sub.SetBounds(22, 46, 720, 40);
        sub.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
        sub.Text = "Checking ollama.com, Hugging Face and GitHub for newer models (about 20 seconds). " +
                   "Only model names are sent. Nothing is downloaded.";
        f.Controls.Add(sub);

        var list = new System.Windows.Forms.Panel();
        list.AutoScroll = true;
        list.BackColor = Panel;
        list.BorderStyle = BorderStyle.FixedSingle;
        list.SetBounds(20, 92, 720, 416);
        list.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
        f.Controls.Add(list);

        var close = new Button();
        close.Text = "Close";
        close.SetBounds(20, 524, 110, 36);
        close.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
        StyleButton(close);
        close.Click += (s, e) => f.Close();
        f.Controls.Add(close);
        var how = new Label();
        how.ForeColor = Dim;
        how.SetBounds(144, 522, 600, 40);
        how.Anchor = AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
        how.Text = "Get downloads a model to this PC after you say yes; it doesn't change which model is used. " +
                   "Fits / Tight / Won't fit use this PC's RAM and graphics card (Check my PC...).";
        f.Controls.Add(how);

        Process dl = null;          // the one download running, if any
        Button dlButton = null;
        string checkedText = "";
        var getButtons = new System.Collections.Generic.List<Button>();

        Action<bool> enableGets = on => { foreach (var b in getButtons) if (b != dlButton && b.Tag != null) b.Enabled = on; };

        // a download: model_check.py --pull TAG (Ollama library) or model_fetch.py hf REPO FILE
        // (Hugging Face, through The Smithy's settings), both printing JSON progress lines
        Action<Button, string[]> startDownload = (btn, a) =>
        {
            string name = a[0] == "pull" ? a[1] : a[1] + " (" + a[2] + ")";
            string gb = a[a.Length - 1];
            btn.Enabled = false;
            sub.Text = "Looking at the free space for " + name + "...";
            ThreadPool.QueueUserWorkItem(_ =>
            {
                string sp = RunPy("model_check.py", "--space " + (a[0] == "pull" ? "ollama" : "hf"));
                try
                {
                    f.BeginInvoke((Action)(() =>
                    {
                        var c = sp.Split('\t');
                        double free = 0, need = 0;
                        double.TryParse(c[0], System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out free);
                        double.TryParse(gb, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out need);
                        string folder = c.Length > 1 ? c[1] : "";
                        if (c.Length < 2) { sub.Text = "Couldn't look at the free space: " + sp; btn.Enabled = true; return; }
                        if (need > 0 && free < need + 1)
                        {
                            sub.Text = "Not enough space for " + name + ": about " + gb + " GB needed, " + c[0] + " GB free in " + folder + ".";
                            btn.Enabled = true;
                            return;
                        }
                        bool yes = AskYesNo(f, "D O W N L O A D",
                            "Download " + name + "?\n\n" +
                            "About " + gb + " GB from " + (a[0] == "pull" ? "the Ollama library" : "Hugging Face") +
                            ", into " + folder + " (" + c[0] + " GB free).\n\n" +
                            "It doesn't change which model " + OthosApp.Name + " or The Smithy uses.",
                            "Yes, download", "Not now", "models");
                        if (!yes) { sub.Text = checkedText; btn.Enabled = true; return; }

                        string script = a[0] == "pull" ? "model_check.py" : "model_fetch.py";
                        string args = a[0] == "pull" ? "--pull " + a[1] : "--config smithy.json hf " + a[1] + " \"" + a[2] + "\"";
                        var psi = new ProcessStartInfo(Path.Combine(OthosApp.AgentDir, ".venv", "Scripts", "python.exe"),
                            "\"" + Path.Combine(OthosApp.AgentDir, script) + "\" " + args);
                        psi.WorkingDirectory = OthosApp.AgentDir;
                        psi.UseShellExecute = false;
                        psi.CreateNoWindow = true;
                        psi.RedirectStandardOutput = true;
                        psi.RedirectStandardError = true;
                        psi.StandardOutputEncoding = System.Text.Encoding.UTF8;
                        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
                        var p = new Process();
                        p.StartInfo = psi;
                        p.EnableRaisingEvents = true;
                        string error = "";
                        bool done = false;
                        string doneNote = "";      // model_fetch.py: an update waiting for a model in use to stop
                        long lastDone = 0;
                        DateTime lastAt = DateTime.Now;
                        double rate = 0;
                        p.OutputDataReceived += (s2, e2) =>
                        {
                            if (e2.Data == null) return;
                            string ev = JsonField(e2.Data, "event");
                            if (ev == "error") error = JsonField(e2.Data, "message");
                            else if (ev == "done") { done = true; doneNote = JsonField(e2.Data, "note"); }
                            else if (ev == "progress")
                            {
                                long d = JsonNumber(e2.Data, "done"), t = JsonNumber(e2.Data, "total");
                                string st = JsonField(e2.Data, "status");
                                double secs = (DateTime.Now - lastAt).TotalSeconds;
                                if (secs >= 1 && d >= lastDone) { rate = (d - lastDone) / secs; lastDone = d; lastAt = DateTime.Now; }
                                string txt = "Downloading " + name + ": " +
                                    (t > 0 ? (d * 100 / t) + "% (" + Gb(d) + " of " + Gb(t) + ")" + (rate > 0 ? ", " + (rate / 1e6).ToString("0.0") + " MB/s" : "")
                                           : st);
                                try { f.BeginInvoke((Action)(() => sub.Text = txt)); } catch { }
                            }
                        };
                        p.Exited += (s2, e2) =>
                        {
                            try { p.WaitForExit(); } catch { }   // the last progress lines first
                            try
                            {
                                f.BeginInvoke((Action)(() =>
                                {
                                    bool stopped = dl == null;     // Cancel cleared it
                                    dl = null;
                                    dlButton = null;
                                    enableGets(true);
                                    if (done && !stopped)
                                    {
                                        btn.Text = "Installed";
                                        btn.ForeColor = Green;
                                        btn.Enabled = false;
                                        btn.Tag = null;
                                        sub.Text = doneNote.Length > 0 ? doneNote
                                            : name + " is downloaded. To use it: Check my PC... (Apply), or The Smithy's model switcher for coding.";
                                    }
                                    else
                                    {
                                        btn.Text = btn.Tag is string && (string)btn.Tag == "update" ? "Update" : "Get";
                                        btn.Enabled = true;
                                        sub.Text = stopped ? "Stopped. What was downloaded is kept, so Get carries on where it left off."
                                                           : "The download didn't finish" + (error.Length > 0 ? ": " + error : ".");
                                    }
                                }));
                            }
                            catch { }
                        };
                        try
                        {
                            p.Start();
                            p.BeginOutputReadLine();
                            p.BeginErrorReadLine();
                            dl = p;
                            dlButton = btn;
                            enableGets(false);
                            btn.Text = "Cancel";
                            btn.Enabled = true;
                            sub.Text = "Starting the download of " + name + "...";
                        }
                        catch (Exception ex) { sub.Text = "Couldn't start the download: " + ex.Message; btn.Enabled = true; }
                    }));
                }
                catch { }
            });
        };

        f.FormClosing += (s, e) =>
        {
            if (dl == null) return;
            if (!AskYesNo(f, "D O W N L O A D", "A download is still running. Stop it?\n\n" +
                    "What was downloaded is kept, so Get carries on where it left off.", "Yes, stop it", "Keep going", "models"))
            { e.Cancel = true; return; }
            try { var p = dl; dl = null; p.Kill(); } catch { }
        };

        // the rows are placed from the list's width, so they wrap again (and the Get buttons stay at the
        // right edge) when the window is resized; the buttons and their download state are kept as they are
        var items = new System.Collections.Generic.List<ModelRow>();
        int lastListW = 0;
        bool laying = false;
        Action relayout = () =>
        {
            if (laying) return;
            laying = true;
            try
            {
                lastListW = list.ClientSize.Width;
                int off = list.AutoScrollPosition.Y;   // 0 or negative: how far the list is scrolled
                int textW = Math.Max(160, list.ClientSize.Width - 230);
                list.SuspendLayout();
                int y = 10;
                foreach (var it in items)
                {
                    if (it.Header != null)
                    {
                        it.Header.SetBounds(10, y + (it.Gap ? 6 : 0) + off, textW + 90, 22);
                        y += it.Gap ? 30 : 24;
                    }
                    else
                    {
                        it.TagLabel.Location = new Point(10, y + off);
                        it.Text.MaximumSize = new Size(textW, 0);
                        it.Text.Location = new Point(94, y + 1 + off);
                        if (it.Button != null) it.Button.Location = new Point(94 + textW + 14, y - 2 + off);
                        y += Math.Max(28, it.Text.PreferredHeight) + 8;
                    }
                }
                list.ResumeLayout(true);
            }
            finally { laying = false; }
        };
        list.Resize += (s, e) => { if (items.Count > 0 && list.ClientSize.Width != lastListW) relayout(); };

        f.Shown += (s, e) => ThreadPool.QueueUserWorkItem(_ =>
        {
            string o = RunPy("model_check.py", "--rows");
            try
            {
                f.BeginInvoke((Action)(() =>
                {
                    list.SuspendLayout();
                    list.Controls.Clear();
                    items.Clear();
                    int n = 0;   // positions are worked out by relayout(), from the list's width
                    string last = "";
                    foreach (var line in o.Replace("\r", "").Split('\n'))
                    {
                        var c = line.Split('\t');
                        if (c.Length < 3) { if (line.Trim().Length > 0) c = new[] { "info", "", line.Trim(), "" }; else continue; }
                        string fit = c[0], area = c[1], text = c[2], action = c.Length > 3 ? c[3].Trim() : "";
                        n++;
                        if (area != last && area.Length > 0)
                        {
                            var a = new Label();
                            a.Text = area;
                            a.ForeColor = Green;
                            a.Font = new Font("Segoe UI Semibold", 10f);
                            list.Controls.Add(a);
                            items.Add(new ModelRow { Header = a, Gap = last.Length > 0 });
                            last = area;
                        }
                        var tag = new Label();
                        tag.Text = fit == "fits" ? "Fits" : fit == "tight" ? "Tight" : fit == "won't fit" ? "Won't fit"
                                 : fit == "update" ? "Update" : "Note";
                        tag.ForeColor = fit == "fits" ? Green : fit == "tight" || fit == "update" ? Gold : fit == "won't fit" ? Red : Dim;
                        tag.BackColor = Bg;
                        tag.TextAlign = ContentAlignment.MiddleCenter;
                        tag.Font = new Font("Segoe UI Semibold", 9f);
                        tag.Size = new Size(74, 22);
                        list.Controls.Add(tag);
                        var t = new Label();
                        t.Text = text;
                        t.ForeColor = Ink;
                        t.AutoSize = true;
                        list.Controls.Add(t);
                        var row = new ModelRow { TagLabel = tag, Text = t };
                        items.Add(row);

                        var act = action.Split('|');
                        if ((act[0] == "pull" && act.Length == 3) || (act[0] == "hf" && act.Length == 4) || (act[0] == "open" && act.Length == 2))
                        {
                            var b = new Button();
                            bool upd = (act[0] == "pull" && text.Contains("an update is out"))
                                       || (act[0] == "hf" && text.Contains("an updated GGUF file is out"));
                            b.Text = act[0] == "open" ? "Open page" : upd ? "Update" : "Get";
                            b.Tag = upd ? "update" : "get";
                            b.Size = new Size(96, 28);
                            row.Button = b;
                            StyleButton(b);
                            if (act[0] == "open")
                            {
                                string url = act[1];
                                b.Click += (s2, e2) => OthosApp.OpenUrl(url);
                            }
                            else
                            {
                                getButtons.Add(b);
                                b.Click += (s2, e2) =>
                                {
                                    if (dl != null && dlButton == b)
                                    {
                                        try { var p = dl; dl = null; p.Kill(); } catch { }
                                        return;
                                    }
                                    if (dl == null) startDownload(b, act);
                                };
                            }
                            list.Controls.Add(b);
                        }
                    }
                    list.ResumeLayout();
                    relayout();
                    int news = ModelNews();
                    checkedText = n == 0 ? "The check didn't run. Is the .venv folder set up (setup.ps1)?"
                             : "Checked " + DateTime.Now.ToString("ddd d MMM, h:mm tt") + ". " +
                               (news > 0 ? news + " new or updated since the last check." : "Nothing new since the last check.") +
                               " Next weekly check in 7 days.";
                    sub.Text = checkedText;
                    modelsRow.Visible = false;
                }));
            }
            catch { }
        });
        WindowFit.Apply(f, new Size(600, 400));   // sizable, with a minimum; opens no bigger than the screen's free area
        f.Show();   // not owned: the Othos window minimizes to the taskbar
    }

    // one row of the models list (see relayout in ShowModels): a group heading, or a tag + text (+ a button)
    class ModelRow
    {
        public Label Header;
        public bool Gap;
        public Label TagLabel, Text;
        public Button Button;
    }

    void StyleButton(Button b)
    {
        b.FlatStyle = FlatStyle.Flat;
        b.FlatAppearance.BorderColor = PurpleLine;
        b.FlatAppearance.MouseOverBackColor = PurpleHover;
        b.FlatAppearance.MouseDownBackColor = PurpleHover;
        b.BackColor = PurpleFill;
        b.ForeColor = Ink;
    }

    static string Gb(long bytes)
    {
        return bytes >= 1000000000 ? (bytes / 1e9).ToString("0.0") + " GB" : (bytes / 1e6).ToString("0") + " MB";
    }

    static string JsonField(string json, string key)
    {
        var m = Regex.Match(json, "\"" + key + "\":\\s*\"((?:[^\"\\\\]|\\\\.)*)\"");
        if (!m.Success) return "";
        try { return Regex.Unescape(m.Groups[1].Value); } catch { return m.Groups[1].Value; }
    }

    static long JsonNumber(string json, string key)
    {
        var m = Regex.Match(json, "\"" + key + "\":\\s*(\\d+)");
        long v;
        return m.Success && long.TryParse(m.Groups[1].Value, out v) ? v : 0;
    }

    int btnY = 200;

    Button MakeButton(string text, int x)
    {
        var b = new Button();
        b.Text = text;
        b.SetBounds(x, btnY, 130, 36);
        b.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
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
        if (File.Exists(SpecsFile)) { WindowState = FormWindowState.Minimized; ModelCheckIfDue(); }   // lives on the taskbar
        else BeginInvoke((Action)(() => { AskSpecsOnce(); WindowState = FormWindowState.Minimized; }));   // one question per start
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
