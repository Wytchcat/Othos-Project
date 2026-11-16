// WorkerApp.cs: the taskbar app of each Othos CPU worker, built twice:
//   Firestarter.exe (site work, worker.py)      build-firestarter.ps1
//   Smithy.exe      (coding, smithy.py)         build-smithy.ps1 (compiled with /define:SMITHY)
// Opening it starts the worker (hidden). Closing its window (X, or right-click the
// taskbar button -> Close window) stops it. It runs on its own, independently of
// the Othos app and of the other worker. The window shows what the worker is doing,
// has the model switcher (applies from the next task), Get models and the work log;
// the Smithy's also has Code chat (Othos's typed code chat, http://127.0.0.1:3000/code);
// Firestarter's also has the Site row (Joomla / WordPress, Site setup), ZOO items and WordPress drafts
// (sends a finished article to WordPress as a draft, only when you press Send), and Check my PC
// (asked once on its first start: reads the PC's specs locally, advises worker.json settings).
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net.Sockets;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

static class FirestarterApp
{
#if SMITHY
    public const string Name = "The Smithy", Title = "T H E   S M I T H Y", Sub = "Othos coding worker",
        Script = "smithy.py", Config = "smithy.json", LogName = "smithy", WorkLog = "Smithy.md",
        MutexName = @"Local\OthosSmithy", Status = @"state\smithy-status.json";
    public static readonly Color Accent = Color.FromArgb(120, 170, 220);          // steel blue
    public static readonly string[][] Tiers = { new[] { "Coding model", "code" } };
#else
    public const string Name = "Firestarter", Title = "F I R E S T A R T E R", Sub = "Othos site-work worker",
        Script = "worker.py", Config = "worker.json", LogName = "worker", WorkLog = "Firestarter.md",
        MutexName = @"Local\OthosFirestarter", Status = @"state\worker-status.json";
    public static readonly Color Accent = Color.FromArgb(255, 140, 60);           // ember orange
    public static readonly string[][] Tiers = { new[] { "Everyday model", "everyday" }, new[] { "Hard model", "hard" } };
#endif
    public static readonly string AgentDir = FindAgentDir();

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
    public static readonly string Py = Path.Combine(AgentDir, @".venv\Scripts\python.exe");
    public static readonly string LogDir = Path.Combine(AgentDir, "logs");
    public static readonly string StatusFile = Path.Combine(AgentDir, Status);

    [STAThread]
    static void Main()
    {
        bool first;
        using (var mutex = new Mutex(true, MutexName, out first))
        {
            if (!first) return;   // already open
            Application.EnableVisualStyles();
            Application.Run(new FirestarterForm());
        }
    }

    // run "python worker.py|smithy.py ..." and return its output (for models / set-model / log)
    public static string Run(string args) { return RunScript(Script + " " + args); }

    // a page of Othos's call-page server in a private browser window (same helper as the Othos app)
    public static void OpenPage(string url)
    {
        var psi = new ProcessStartInfo("powershell.exe",
            "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command \". '" +
            Path.Combine(AgentDir, "open-private.ps1") + "'; Open-Private '" + url + "'\"");
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
                bool ok = r.AsyncWaitHandle.WaitOne(1000) && c.Connected;
                if (ok) c.EndConnect(r);
                return ok;
            }
        }
        catch { return false; }
    }

    // the downloader, with this worker's settings
    public static string Fetch(string args) { return "model_fetch.py --config " + Config + " " + args; }

    // start "python <script> ..." with its output readable line by line (downloads)
    public static Process Start(string scriptAndArgs)
    {
        var psi = new ProcessStartInfo(Py, "-u " + scriptAndArgs);
        psi.WorkingDirectory = AgentDir;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true;
        psi.StandardOutputEncoding = System.Text.Encoding.UTF8;
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        return Process.Start(psi);
    }

    // like Run, but hands `input` to the script on stdin (an API key: never on a command line,
    // where other programs could see it)
    public static string RunWithInput(string args, string input)
    {
        var psi = new ProcessStartInfo(Py, Script + " " + args);
        psi.WorkingDirectory = AgentDir;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardInput = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        psi.StandardOutputEncoding = System.Text.Encoding.UTF8;
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        using (var p = Process.Start(psi))
        {
            p.StandardInput.WriteLine(input);
            p.StandardInput.Close();
            string o = p.StandardOutput.ReadToEnd() + p.StandardError.ReadToEnd();
            p.WaitForExit(30000);
            return o.Trim();
        }
    }

    public static string RunScript(string scriptAndArgs)
    {
        var psi = new ProcessStartInfo(Py, scriptAndArgs);
        psi.WorkingDirectory = AgentDir;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true;
        psi.StandardOutputEncoding = System.Text.Encoding.UTF8;
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        using (var p = Process.Start(psi))
        {
            string o = p.StandardOutput.ReadToEnd();
            p.WaitForExit(30000);
            return o;
        }
    }

    // keep the previous run's log in logs\old (newest 20), like Start Othos
    public static void SaveOldLog()
    {
        try
        {
            string log = Path.Combine(LogDir, LogName + ".log");
            if (!File.Exists(log) || new FileInfo(log).Length == 0) return;
            string old = Path.Combine(LogDir, "old");
            Directory.CreateDirectory(old);
            File.Move(log, Path.Combine(old, LogName + "-" + File.GetLastWriteTime(log).ToString("yyyy-MM-dd-HHmmss") + ".log"));
            var files = new List<string>(Directory.GetFiles(old, LogName + "-*.log"));
            files.Sort((a, b) => File.GetLastWriteTime(b).CompareTo(File.GetLastWriteTime(a)));
            for (int i = 20; i < files.Count; i++) File.Delete(files[i]);
        }
        catch { }
    }
}

// No fixed-size windows (the user's rule): every window here is sizable, can't open bigger than the screen's
// free area (WindowFit.cs), and its controls follow the size. AutoAnchor sets each control's Anchor from where
// it sits, once all the controls are in (the Load event): wide ones stretch, ones by the right edge stay right,
// the biggest list or text box grows with the height, and whatever is below it stays at the bottom. A control
// that already has its own Anchor (not the default Top, Left) is left as it is. Nov 14.
static class AutoAnchor
{
    public static void Fit(Form f, bool dialog)
    {
        f.Load += (s, e) =>
        {
            Size open = f.ClientSize;
            Apply(f);
            if (dialog) WindowFit.ApplyDialog(f, open); else WindowFit.Apply(f, open);
        };
    }

    public static void Apply(Control parent)
    {
        int w = parent.ClientSize.Width;
        Control grower = null;
        foreach (Control c in parent.Controls)
            if ((c is ListBox || c is ListView || c is RichTextBox || (c is TextBox && ((TextBox)c).Multiline) ||
                 c is DataGridView || c is TreeView || (c is Panel && ((Panel)c).AutoScroll))
                && (grower == null || c.Height > grower.Height))
                grower = c;
        foreach (Control c in parent.Controls)
        {
            if (c.Anchor != (AnchorStyles.Top | AnchorStyles.Left)) continue;      // laid out on purpose already
            if (c.Dock != DockStyle.None) continue;
            AnchorStyles a = 0;
            bool wide = c.Width >= w * 0.5;
            bool right = !wide && c.Left > w / 2 && w - c.Right <= 40;
            a |= wide ? AnchorStyles.Left | AnchorStyles.Right : right ? AnchorStyles.Right : AnchorStyles.Left;
            if (c.Top <= 2 && c.Bottom >= parent.ClientSize.Height - 2) a |= AnchorStyles.Top | AnchorStyles.Bottom;   // a side stripe
            else if (grower != null && c == grower) a |= AnchorStyles.Top | AnchorStyles.Bottom;
            else if (grower != null && c.Top >= grower.Bottom - 2) a |= AnchorStyles.Bottom;
            else a |= AnchorStyles.Top;
            c.Anchor = a;
            if (c is Panel || c is GroupBox) Apply(c);                              // the same inside
        }
    }
}

// Pop-ups in the colours of the window they come from (the user, Nov 13: "All popups match the
// color scheme of the logo/ui it is from"): the dark background, the app's own accent (the
// Smithy's steel blue, Firestarter's ember) on a stripe and the main button, gold for a warning,
// and the purple buttons, in place of Windows' white message box. Same calls and answers as
// MessageBox.Show; closing it with X counts as No.
static class ThemedBox
{
    static readonly Color Bg = Color.FromArgb(2, 7, 5), Ink = Color.FromArgb(200, 230, 210),
        Gold = Color.FromArgb(231, 195, 104), PurpleFill = Color.FromArgb(14, 16, 25),
        PurpleLine = Color.FromArgb(64, 53, 105), PurpleHover = Color.FromArgb(25, 24, 43);

    public static DialogResult Show(IWin32Window owner, string text, string caption)
    { return Show(owner, text, caption, MessageBoxButtons.OK, MessageBoxIcon.None, MessageBoxDefaultButton.Button1); }

    public static DialogResult Show(IWin32Window owner, string text, string caption, MessageBoxButtons buttons, MessageBoxIcon icon)
    { return Show(owner, text, caption, buttons, icon, MessageBoxDefaultButton.Button1); }

    public static DialogResult Show(IWin32Window owner, string text, string caption, MessageBoxButtons buttons,
                                    MessageBoxIcon icon, MessageBoxDefaultButton def)
    { return Show(owner, text, caption, buttons, icon, def, null); }

    // answeredElsewhere: checked every 2 seconds; when it's true (answered by voice, say), the box closes by
    // itself with Cancel, so the same question is never answered twice (Nov 14, suggestion 2)
    public static DialogResult Show(IWin32Window owner, string text, string caption, MessageBoxButtons buttons,
                                    MessageBoxIcon icon, MessageBoxDefaultButton def, Func<bool> answeredElsewhere)
    {
        bool yesNo = buttons == MessageBoxButtons.YesNo;
        Color accent = icon == MessageBoxIcon.Warning ? Gold : FirestarterApp.Accent;
        using (var f = new Form())
        {
            f.Text = caption;
            f.FormBorderStyle = FormBorderStyle.FixedDialog; f.MaximizeBox = false; f.MinimizeBox = false;
            f.ShowInTaskbar = false;
            try { f.Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
            f.BackColor = Bg; f.ForeColor = Ink; f.Font = new Font("Segoe UI", 10f);

            var msg = new Label(); msg.Text = text; msg.ForeColor = Ink; msg.AutoSize = true;
            msg.MaximumSize = new Size(430, 0); msg.Location = new Point(30, 22);
            f.Controls.Add(msg);
            int h = Math.Max(msg.GetPreferredSize(new Size(430, 0)).Height, 22);
            int w = 490, by = 22 + h + 22;

            Func<string, DialogResult, int, bool, Button> make = (t, r, x, main) =>
            {
                var b = new Button(); b.Text = t; b.DialogResult = r; b.SetBounds(x, by, 100, 30);
                b.FlatStyle = FlatStyle.Flat; b.BackColor = PurpleFill; b.ForeColor = Ink;
                b.FlatAppearance.BorderColor = main ? accent : PurpleLine; b.FlatAppearance.MouseOverBackColor = PurpleHover;
                f.Controls.Add(b); return b;
            };
            Button first, second = null;
            if (yesNo)
            {
                first = make("Yes", DialogResult.Yes, w - 24 - 210, def != MessageBoxDefaultButton.Button2);
                second = make("No", DialogResult.No, w - 24 - 100, def == MessageBoxDefaultButton.Button2);
                f.CancelButton = second;
            }
            else { first = make("OK", DialogResult.OK, w - 24 - 100, true); f.CancelButton = first; }
            f.AcceptButton = def == MessageBoxDefaultButton.Button2 && second != null ? second : first;
            f.ClientSize = new Size(w, by + 30 + 18);

            var stripe = new Panel(); stripe.BackColor = accent; stripe.SetBounds(0, 0, 6, f.ClientSize.Height);
            f.Controls.Add(stripe);
            f.Shown += (s, e) => ((Button)f.AcceptButton).Focus();
            AutoAnchor.Fit(f, true);                    // a message box can be resized too

            // the worker windows live minimized on the taskbar: then centre on the screen, in front
            var o = owner as Form;
            bool hidden = o == null || !o.Visible || o.WindowState == FormWindowState.Minimized;
            f.StartPosition = hidden ? FormStartPosition.CenterScreen : FormStartPosition.CenterParent;
            f.TopMost = hidden;
            System.Windows.Forms.Timer watch = null;
            if (answeredElsewhere != null)
            {
                watch = new System.Windows.Forms.Timer(); watch.Interval = 2000;
                watch.Tick += (s, e) => { bool done = false; try { done = answeredElsewhere(); } catch { }
                                          if (done) { watch.Stop(); f.DialogResult = DialogResult.Cancel; } };
                f.Shown += (s, e) => watch.Start();
            }
            try { return hidden ? f.ShowDialog() : f.ShowDialog(owner); }
            finally { if (watch != null) watch.Dispose(); }
        }
    }
}

// "Which antivirus do you use?" (Nov 16, download_scan.py): shown when model_fetch says av_needed
// (Defender is off and nothing is chosen). A themed, resizable drop-down of the products, the
// installed one picked first, each with its price and what Othos can do with it; "Something else"
// and "None" come last. OK saves the pick (download_scan.py choose); Escape, X or Cancel saves
// nothing, so it's asked again next time.
static class AntivirusPicker
{
    static readonly Color Bg = Color.FromArgb(2, 7, 5), Ink = Color.FromArgb(200, 230, 210),
        Dim = Color.FromArgb(120, 140, 130), PurpleFill = Color.FromArgb(14, 16, 25),
        PurpleLine = Color.FromArgb(64, 53, 105), PurpleHover = Color.FromArgb(25, 24, 43);

    static string S(Dictionary<string, object> d, string k)
    { return d != null && d.ContainsKey(k) && d[k] != null ? Convert.ToString(d[k]) : ""; }

    // the chosen name, or null when nothing was chosen
    public static string Ask(IWin32Window owner, System.Collections.IEnumerable products)
    {
        var list = new List<Dictionary<string, object>>();
        foreach (var o in products) { var d = o as Dictionary<string, object>; if (d != null && S(d, "name") != "") list.Add(d); }
        if (list.Count == 0) return null;
        using (var f = new Form())
        {
            f.Text = FirestarterApp.Name + ": antivirus";
            f.FormBorderStyle = FormBorderStyle.FixedDialog; f.MaximizeBox = false; f.MinimizeBox = false;
            f.ShowInTaskbar = false;
            try { f.Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
            f.BackColor = Bg; f.ForeColor = Ink; f.Font = new Font("Segoe UI", 10f);
            f.ClientSize = new Size(500, 236);

            var msg = new Label(); msg.ForeColor = Ink; msg.SetBounds(30, 18, 450, 44);
            msg.Text = "Microsoft Defender isn't running, so downloads can't be scanned. Which antivirus do you use?";
            f.Controls.Add(msg);
            var pick = new ComboBox(); pick.DropDownStyle = ComboBoxStyle.DropDownList; pick.FlatStyle = FlatStyle.Flat;
            pick.BackColor = PurpleFill; pick.ForeColor = Ink; pick.SetBounds(30, 68, 450, 28);
            int start = -1;
            for (int i = 0; i < list.Count; i++)
            {
                var d = list[i]; bool have = Convert.ToBoolean(d.ContainsKey("installed") ? d["installed"] : false);
                pick.Items.Add(S(d, "name") + (S(d, "price") != "" ? "   (" + S(d, "price") + ")" : "") + (have ? "   installed" : ""));
                if (have && start < 0) start = i;
            }
            pick.SelectedIndex = start >= 0 ? start : 0;
            f.Controls.Add(pick);
            var note = new Label(); note.ForeColor = Dim; note.SetBounds(30, 104, 450, 66); f.Controls.Add(note);
            EventHandler show = (s, e) => { note.Text = S(list[pick.SelectedIndex], "note"); };
            pick.SelectedIndexChanged += show; show(null, null);

            Func<string, DialogResult, int, bool, Button> make = (t, r, x, main) =>
            {
                var b = new Button(); b.Text = t; b.DialogResult = r; b.SetBounds(x, 186, 100, 30);
                b.FlatStyle = FlatStyle.Flat; b.BackColor = PurpleFill; b.ForeColor = Ink;
                b.FlatAppearance.BorderColor = main ? FirestarterApp.Accent : PurpleLine;
                b.FlatAppearance.MouseOverBackColor = PurpleHover; f.Controls.Add(b); return b;
            };
            var ok = make("OK", DialogResult.OK, 500 - 24 - 210, true);
            var cancel = make("Cancel", DialogResult.Cancel, 500 - 24 - 100, false);
            f.AcceptButton = ok; f.CancelButton = cancel;
            var stripe = new Panel(); stripe.BackColor = FirestarterApp.Accent; stripe.SetBounds(0, 0, 6, f.ClientSize.Height);
            f.Controls.Add(stripe);
            f.Shown += (s, e) => pick.Focus();
            AutoAnchor.Fit(f, true);                        // sizable, like every window
            var o2 = owner as Form;
            bool hidden = o2 == null || !o2.Visible || o2.WindowState == FormWindowState.Minimized;
            f.StartPosition = hidden ? FormStartPosition.CenterScreen : FormStartPosition.CenterParent;
            f.TopMost = hidden;
            var answer = hidden ? f.ShowDialog() : f.ShowDialog(owner);
            return answer == DialogResult.OK ? S(list[pick.SelectedIndex], "name") : null;
        }
    }
}

// "Get models" window: Hugging Face (GGUF), the Ollama library, or a direct https
// link to a .gguf. The work is done by model_fetch.py, which prints progress lines.
class DownloaderForm : Form
{
    static readonly Color Bg = Color.FromArgb(2, 7, 5), Ink = Color.FromArgb(200, 230, 210),
        Dim = Color.FromArgb(120, 140, 130), Ember = FirestarterApp.Accent, Gold = Color.FromArgb(231, 195, 104),
        PurpleFill = Color.FromArgb(14, 16, 25), PurpleLine = Color.FromArgb(64, 53, 105), PurpleHover = Color.FromArgb(25, 24, 43);
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer() { MaxJsonLength = 50000000 };

    readonly RadioButton srcHf = new RadioButton(), srcOllama = new RadioButton(), srcUrl = new RadioButton();
    readonly TextBox query = new TextBox();
    readonly Button search = new Button(), download = new Button(), cancel = new Button();
    readonly ListBox repos = new ListBox(), files = new ListBox();
    readonly Label hint = new Label(), info = new Label(), status = new Label();
    readonly ProgressBar bar = new ProgressBar();
    List<Dictionary<string, object>> repoList = new List<Dictionary<string, object>>(), fileList = new List<Dictionary<string, object>>();
    long free; string backend = "", folder = "";
    Process job;

    public DownloaderForm()
    {
        Text = FirestarterApp.Name + ": get models";
        // checking for new models re-checks the providers' free allowances too (worker_cloud.py free-limits;
        // only free account pages, no test requests), so the Providers window's order stays right
        ThreadPool.QueueUserWorkItem(_ => { try { FirestarterApp.Run("free-limits"); } catch { } });
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        FormBorderStyle = FormBorderStyle.FixedSingle; MaximizeBox = false; MinimizeBox = false;
        StartPosition = FormStartPosition.CenterParent;
        ClientSize = new Size(620, 560);
        AutoAnchor.Fit(this, true);                     // sizable, fits the screen (no fixed-size windows)
        BackColor = Bg; ForeColor = Ink; Font = new Font("Segoe UI", 10f);

        int x = 20;
        foreach (var r in new[] { Tuple.Create(srcHf, "Hugging Face"), Tuple.Create(srcOllama, "Ollama library"), Tuple.Create(srcUrl, "Direct link") })
        {
            r.Item1.Text = r.Item2; r.Item1.ForeColor = Ink; r.Item1.SetBounds(x, 14, 150, 26);
            r.Item1.CheckedChanged += (s, e) => { if (((RadioButton)s).Checked) SwitchSource(); };
            Controls.Add(r.Item1); x += 160;
        }
        hint.SetBounds(20, 44, 580, 22); hint.ForeColor = Dim; Controls.Add(hint);
        query.SetBounds(20, 70, 470, 28); query.BackColor = PurpleFill; query.ForeColor = Ink; query.BorderStyle = BorderStyle.FixedSingle;
        query.KeyDown += (s, e) => { if (e.KeyCode == Keys.Enter) { e.SuppressKeyPress = true; if (srcHf.Checked) DoSearch(); else UpdateInfo(); } };
        query.TextChanged += (s, e) => { if (!srcHf.Checked) UpdateInfo(); };
        Controls.Add(query);
        Style(search, "Search", 500, 69, 100); search.Click += (s, e) => DoSearch();

        repos.SetBounds(20, 108, 580, 150); files.SetBounds(20, 268, 580, 150);
        foreach (var lb in new[] { repos, files })
        {
            lb.BackColor = PurpleFill; lb.ForeColor = Ink; lb.BorderStyle = BorderStyle.FixedSingle;
            lb.IntegralHeight = false; Controls.Add(lb);
        }
        repos.SelectedIndexChanged += (s, e) => LoadFiles();
        files.SelectedIndexChanged += (s, e) => UpdateInfo();

        info.SetBounds(20, 426, 580, 44); Controls.Add(info);
        bar.SetBounds(20, 474, 580, 14); bar.Maximum = 1000; Controls.Add(bar);
        status.SetBounds(20, 492, 580, 22); status.ForeColor = Dim; Controls.Add(status);
        Style(download, "Download", 380, 516, 105); download.Click += (s, e) => StartDownload();
        Style(cancel, "Cancel", 495, 516, 105); cancel.Enabled = false;
        cancel.Click += (s, e) => StopJob("Cancelled. Starting the same download again resumes it.");

        srcHf.Checked = true;
        ThreadPool.QueueUserWorkItem(_ =>
        {
            var t = Parse(FirestarterApp.RunScript(FirestarterApp.Fetch("target")));
            try { BeginInvoke((Action)(() => {
                if (t != null) { backend = Str(t, "backend"); folder = Str(t, "folder"); free = Convert.ToInt64(t["free"]); }
                UpdateInfo(); })); } catch { }
        });
    }

    void Style(Button b, string text, int x, int y, int w)
    {
        b.Text = text; b.SetBounds(x, y, w, 32); b.FlatStyle = FlatStyle.Flat;
        b.FlatAppearance.BorderColor = PurpleLine; b.FlatAppearance.MouseOverBackColor = PurpleHover;
        b.BackColor = PurpleFill; b.ForeColor = Ink; Controls.Add(b);
    }

    static string Str(Dictionary<string, object> d, string k) { return d != null && d.ContainsKey(k) && d[k] != null ? Convert.ToString(d[k]) : ""; }
    static string Gb(double bytes) { return (bytes / 1e9).ToString(bytes < 1e10 ? "0.0" : "0") + " GB"; }
    static Dictionary<string, object> Parse(string s) { try { return Json.Deserialize<Dictionary<string, object>>(s); } catch { return null; } }

    void SwitchSource()
    {
        bool hf = srcHf.Checked;
        search.Visible = repos.Visible = files.Visible = hf;
        query.Text = "";
        hint.Text = hf ? "Search Hugging Face for GGUF models (★ = well-known publisher), then pick a file."
                  : srcOllama.Checked ? "Ollama library model name, e.g. qwen3:14b or gemma3:4b (see ollama.com/library)."
                  : "An https:// link to a .gguf file. Other file types aren't accepted.";
        UpdateInfo();
    }

    void DoSearch()
    {
        if (query.Text.Trim() == "") return;
        status.Text = "Searching…"; repos.Items.Clear(); files.Items.Clear();
        string q = query.Text.Trim().Replace("\"", "");
        ThreadPool.QueueUserWorkItem(_ =>
        {
            string o = FirestarterApp.RunScript(FirestarterApp.Fetch("search \"" + q + "\""));
            List<Dictionary<string, object>> list = null;
            try { list = Json.Deserialize<List<Dictionary<string, object>>>(o); } catch { }
            try { BeginInvoke((Action)(() => {
                repoList = list ?? new List<Dictionary<string, object>>();
                foreach (var r in repoList)
                    repos.Items.Add((Convert.ToBoolean(r["trusted"]) ? "★ " : "   ") + Str(r, "id") + "   (" +
                        Convert.ToInt64(r["downloads"]).ToString("N0") + " downloads" + (Convert.ToBoolean(r["gated"]) ? ", needs a token" : "") + ")");
                status.Text = list == null ? "Search failed: " + (Parse(o) != null ? Str(Parse(o), "message") : "no answer")
                                           : repoList.Count + " models found";
            })); } catch { }
        });
    }

    void LoadFiles()
    {
        files.Items.Clear(); fileList.Clear(); UpdateInfo();
        if (repos.SelectedIndex < 0) return;
        string repo = Str(repoList[repos.SelectedIndex], "id");
        status.Text = "Listing files…";
        ThreadPool.QueueUserWorkItem(_ =>
        {
            string o = FirestarterApp.RunScript(FirestarterApp.Fetch("files \"" + repo + "\""));
            List<Dictionary<string, object>> list = null;
            try { list = Json.Deserialize<List<Dictionary<string, object>>>(o); } catch { }
            try { BeginInvoke((Action)(() => {
                fileList = list ?? new List<Dictionary<string, object>>();
                foreach (var f in fileList)
                {
                    int parts = ((System.Collections.ArrayList)f["files"]).Count;
                    files.Items.Add(Str(f, "name") + "   " + Gb(Convert.ToDouble(f["size"])) +
                        (parts > 1 ? "   (" + parts + " parts)" : "") + (Convert.ToBoolean(f["mmproj"]) ? "   (vision add-on, not a model)" : ""));
                }
                status.Text = list == null ? "Couldn't list files: " + Str(Parse(o), "message") : fileList.Count + " .gguf files";
            })); } catch { }
        });
    }

    double SelectedSize()
    {
        return srcHf.Checked && files.SelectedIndex >= 0 ? Convert.ToDouble(fileList[files.SelectedIndex]["size"]) : 0;
    }

    void UpdateInfo()
    {
        string where = backend == "ollama" ? "Goes into Ollama (" + folder + ")" : backend == "" ? "" : "Goes into " + folder;
        double size = SelectedSize();
        info.Text = where + (free > 0 ? "\nFree space: " + Gb(free) : "") + (size > 0 ? "     Download size: " + Gb(size) : "");
        bool fits = size == 0 || size + 1e9 < free;
        info.ForeColor = fits ? Ink : Gold;
        if (!fits) info.Text += "     Not enough free space";
        bool ready = srcHf.Checked ? files.SelectedIndex >= 0 && !Convert.ToBoolean(fileList[files.SelectedIndex]["mmproj"])
                                   : query.Text.Trim() != "";
        download.Enabled = job == null && ready && fits;
    }

    void StartDownload()
    {
        string args;
        if (srcHf.Checked) args = "hf \"" + Str(repoList[repos.SelectedIndex], "id") + "\" \"" + Str(fileList[files.SelectedIndex], "name") + "\"";
        else if (srcOllama.Checked) args = "ollama \"" + query.Text.Trim().Replace("\"", "") + "\"";
        else args = "url \"" + query.Text.Trim().Replace("\"", "") + "\"";
        bar.Value = 0; status.ForeColor = Dim; status.Text = "Starting…";
        download.Enabled = false; cancel.Enabled = true;
        job = FirestarterApp.Start(FirestarterApp.Fetch(args));
        var p = job;
        ThreadPool.QueueUserWorkItem(_ =>
        {
            string line, final = null; bool ok = false;
            while ((line = p.StandardOutput.ReadLine()) != null)
            {
                var ev = Parse(line);
                if (ev == null) continue;
                string kind = Str(ev, "event");
                if (kind == "done") { ok = true; final = "Done: " + Str(ev, "model") + " is ready in the model switcher."; }
                else if (kind == "error") final = "Failed: " + Str(ev, "message");
                else if (kind == "av_needed" && ev.ContainsKey("products") && ev["products"] is System.Collections.IEnumerable)
                {
                    // Defender is off and no antivirus is chosen: ask which (download_scan.py); this
                    // download carries on, the next one uses the pick
                    var prods = (System.Collections.IEnumerable)ev["products"];
                    try { BeginInvoke((Action)(() => {
                        string name = AntivirusPicker.Ask(this, prods);
                        if (name == null) return;
                        ThreadPool.QueueUserWorkItem(__ => {
                            string r = FirestarterApp.RunScript("download_scan.py choose \"" + name.Replace("\"", "") + "\"");
                            bool saved = Parse(r) != null && Str(Parse(r), "chosen") == name;
                            try { BeginInvoke((Action)(() => {
                                if (!saved) ThemedBox.Show(this, "Couldn't save the antivirus choice. It will be asked again next time.",
                                                           FirestarterApp.Name + ": antivirus", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                            })); } catch { }
                        });
                    })); } catch { }
                }
                else if (kind == "progress")
                {
                    double done = Convert.ToDouble(ev["done"]), total = Convert.ToDouble(ev["total"]);
                    string st = Str(ev, "status");
                    try { BeginInvoke((Action)(() => {
                        bar.Value = total > 0 ? (int)Math.Min(1000, done * 1000 / total) : 0;
                        status.Text = st + (total > 0 ? "   " + Gb(done) + " of " + Gb(total) : "");
                    })); } catch { }
                }
            }
            p.WaitForExit();
            try { BeginInvoke((Action)(() => {
                if (job != p) return;   // cancelled
                job = null; cancel.Enabled = false;
                status.ForeColor = ok ? Ember : Gold;
                status.Text = final ?? "Stopped.";
                if (ok) bar.Value = bar.Maximum;
                UpdateInfo();
                // a file the virus scan or the file-type check stopped (download_scan.py): said in a box, not
                // only on the status line
                if (!ok && final != null && final.StartsWith("Failed: ") && System.Text.RegularExpressions.Regex.IsMatch(final, "quarantine|set aside|threat|isn't a real GGUF|disappeared", System.Text.RegularExpressions.RegexOptions.IgnoreCase))
                    ThemedBox.Show(this, "This download was blocked and not used.\n\n" + final.Substring("Failed: ".Length),
                                   FirestarterApp.Name + ": download blocked", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            })); } catch { }
        });
    }

    void StopJob(string message)
    {
        var p = job; job = null;
        if (p != null && !p.HasExited)
        {
            try { var k = new ProcessStartInfo("taskkill", "/PID " + p.Id + " /T /F"); k.CreateNoWindow = true; k.UseShellExecute = false; Process.Start(k).WaitForExit(10000); } catch { }
        }
        cancel.Enabled = false; status.ForeColor = Gold; status.Text = message; UpdateInfo();
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        if (job != null) StopJob("Cancelled.");
        base.OnFormClosing(e);
    }
}

// "Providers" window (both workers): other model providers by API key, so nobody needs to edit
// .env or the settings by hand. Local ones first. A key is typed into a hidden box, handed to
// the worker's script on stdin (worker_cloud.py: set-key) and saved encrypted by Windows
// (worker_keys.py, %LOCALAPPDATA%\Othos\keys.dat); only its last 4 characters are ever shown.
// Turning on a free tier that may train on what's sent asks Yes/No first.
class ProvidersForm : Form
{
    static readonly Color Bg = Color.FromArgb(2, 7, 5), Ink = Color.FromArgb(200, 230, 210),
        Dim = Color.FromArgb(120, 140, 130), Gold = Color.FromArgb(231, 195, 104), Ok = Color.FromArgb(120, 200, 140),
        PurpleFill = Color.FromArgb(14, 16, 25), PurpleLine = Color.FromArgb(64, 53, 105), PurpleHover = Color.FromArgb(25, 24, 43);
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    readonly Panel body = new Panel();
    readonly TextBox tokensDay = new TextBox(), usdDay = new TextBox();
    readonly Label footer = new Label();
    bool filling, refreshed;
    string nextFooter = "";
    [System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    static extern IntPtr SendMessage(IntPtr h, int msg, IntPtr w, string l);
    static void Hint(TextBox b, string text) { b.HandleCreated += (s, e) => SendMessage(b.Handle, 0x1501, (IntPtr)1, text); }  // EM_SETCUEBANNER

    public ProvidersForm()
    {
        Text = FirestarterApp.Name + ": providers";
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        FormBorderStyle = FormBorderStyle.FixedSingle; MaximizeBox = false; MinimizeBox = false;
        StartPosition = FormStartPosition.CenterParent;
        ClientSize = new Size(760, 640);
        AutoAnchor.Fit(this, true);
        BackColor = Bg; ForeColor = Ink; Font = new Font("Segoe UI", 9.5f);

        var head = new Label();
        head.Text = "Local first: these run on this PC and nothing leaves it. Cloud providers get only what a task " +
                    "would give the local model, after the redaction filter, and only while switched On. " +
                    "Switch order 1, 2, 3...: used one after another as free tokens run out, then this PC.";
        head.ForeColor = Dim; head.SetBounds(16, 8, 728, 56);
        Controls.Add(head);
        body.SetBounds(10, 66, 740, 484); body.AutoScroll = true; body.BackColor = Bg;
        Controls.Add(body);

        AddLabel(this, "Limit per day:", 16, 564, 140, Dim);
        tokensDay.SetBounds(160, 561, 100, 24); StyleBox(tokensDay); Controls.Add(tokensDay);
        AddLabel(this, "tokens each,", 264, 564, 90, Dim);
        usdDay.SetBounds(356, 561, 60, 24); StyleBox(usdDay); Controls.Add(usdDay);
        AddLabel(this, "dollars in all (dollars count once prices are set)", 420, 564, 330, Dim);
        var saveLimits = MakeButton(this, "Save limits", 16, 596, 120);
        saveLimits.Click += (s, e) => SaveLimits();
        var close = MakeButton(this, "Close", 624, 596, 120);
        close.Click += (s, e) => Close();
        // Refresh: reads .env and the saved keys again, so a key added to .env shows here (and in the
        // Smithy's Provider list) without closing anything; each task reads the keys fresh anyway
        var refresh = MakeButton(this, "Refresh", 498, 596, 120);
        refresh.Click += (s, e) =>
        {
            footer.Text = "Checking free allowances…";
            ThreadPool.QueueUserWorkItem(_ => { FirestarterApp.Run("free-limits");
                try { BeginInvoke((Action)(() => { refreshed = true; Reload(); })); } catch { } });
        };
        // the switch order from the ranking: providers that are On, ready and give something free
        var sort = MakeButton(this, "Sort by free allowance", 142, 596, 180);
        sort.Click += (s, e) => ThreadPool.QueueUserWorkItem(_ => { FirestarterApp.Run("switch-sort");
            try { BeginInvoke((Action)(() => { nextFooter = "Switch order sorted, most free first."; Reload(); })); } catch { } });
        footer.SetBounds(328, 602, 166, 40); footer.ForeColor = Dim; Controls.Add(footer);
        Reload();
    }

    static void StyleBox(TextBox t) { t.BackColor = PurpleFill; t.ForeColor = Ink; t.BorderStyle = BorderStyle.FixedSingle; }

    static Label AddLabel(Control parent, string text, int x, int y, int w, Color c, bool bold = false)
    {
        var l = new Label(); l.Text = text; l.ForeColor = c; l.SetBounds(x, y, w, 20);
        if (bold) l.Font = new Font("Segoe UI Semibold", 10f);
        parent.Controls.Add(l); return l;
    }

    static Button MakeButton(Control parent, string text, int x, int y, int w)
    {
        var b = new Button(); b.Text = text; b.SetBounds(x, y, w, 26); b.FlatStyle = FlatStyle.Flat;
        b.FlatAppearance.BorderColor = PurpleLine; b.FlatAppearance.MouseOverBackColor = PurpleHover;
        b.BackColor = PurpleFill; b.ForeColor = Ink; parent.Controls.Add(b); return b;
    }

    static string S(Dictionary<string, object> d, string k) { return d != null && d.ContainsKey(k) && d[k] != null ? Convert.ToString(d[k]) : ""; }
    static bool B(Dictionary<string, object> d, string k) { return d != null && d.ContainsKey(k) && d[k] is bool && (bool)d[k]; }

    void Reload()
    {
        footer.Text = "Reading…";
        ThreadPool.QueueUserWorkItem(_ =>
        {
            Dictionary<string, object> m = null;
            try { m = Json.Deserialize<Dictionary<string, object>>(FirestarterApp.Run("providers")); } catch { }
            try { BeginInvoke((Action)(() => Fill(m))); } catch { }
        });
    }

    void Fill(Dictionary<string, object> m)
    {
        filling = true;
        body.Controls.Clear();
        if (m == null) { footer.Text = "Couldn't read the providers."; filling = false; return; }
        tokensDay.Text = S(m, "max_tokens_per_day"); usdDay.Text = S(m, "max_usd_per_day");
        int y = 6;
        string section = "";
        foreach (Dictionary<string, object> p in (System.Collections.ArrayList)m["providers"])
        {
            string where = S(p, "where");
            string sec = where == "local" ? "On this PC" : B(p, "paid_only") ? "Cloud: paid only"
                       : "Cloud: free allowance, most to least";
            if (sec != section) { section = sec; AddLabel(body, sec.ToUpper(), 6, y, 400, Gold, true); y += 26; }
            y += AddRow(p, y);
        }
        footer.Text = nextFooter != "" ? nextFooter : refreshed ? "Read .env and saved keys again at " + DateTime.Now.ToString("h:mm tt") + "." : "";
        nextFooter = "";
        refreshed = false;
        filling = false;
    }

    // returns the row's height
    int AddRow(Dictionary<string, object> p, int y)
    {
        string name = S(p, "name"), label = S(p, "label"), where = S(p, "where");
        bool needsKey = B(p, "needs_key"), trains = B(p, "trains"), cloud = where == "cloud";
        var title = AddLabel(body, label + (where == "proxy" ? "  (on this PC, then cloud)" : ""), 6, y, 300, Ink, true);
        title.AutoSize = true;
        string exp = S(p, "key_expires");
        if (exp != "")                                  // keys that report an end date (Cloudflare, OpenRouter, Hugging Face)
            AddLabel(body, "key ends " + exp, title.Right + 8, y + 1, 180, B(p, "key_expires_soon") ? Gold : Dim);
        int line = y + 20;
        if (cloud)
        {
            // what it gives away (live figure when known), a link to its own limits page
            string live = S(p, "free_live"), free = S(p, "free"), src = S(p, "free_source");
            string text = B(p, "paid_only") ? "Paid only" + (live != "" ? ": " + live : "")
                        : "Free: " + (live != "" ? live : free + (S(p, "free_checked") != "" ? " (checked " + Checked(S(p, "free_checked")) + ")" : ""))
                          + (B(p, "free_stale") ? ", worth re-checking" : "");
            var fl = new LinkLabel(); fl.Text = text; fl.SetBounds(6, line, 540, 20); fl.AutoEllipsis = true;
            fl.LinkColor = fl.ActiveLinkColor = fl.VisitedLinkColor = B(p, "free_stale") ? Gold : B(p, "paid_only") ? Dim : Ok;
            fl.LinkBehavior = LinkBehavior.HoverUnderline;
            if (src != "") fl.Click += (s, e) => { try { Process.Start(src); } catch { } };
            body.Controls.Add(fl);
            line += 20;
        }
        string note = S(p, "note") + (trains ? " Its free tier may use what's sent for training." : "");
        bool addSecond = B(p, "can_add_second");      // a second account's key (Nov 16: a personal and a business one)
        var n = AddLabel(body, note, 6, line, addSecond ? 550 : 690, trains ? Gold : Dim);
        if (addSecond)
        {
            var more = new LinkLabel(); more.Text = "Add a 2nd key"; more.LinkColor = more.ActiveLinkColor = more.VisitedLinkColor = Gold;
            more.LinkBehavior = LinkBehavior.HoverUnderline; more.SetBounds(560, line, 136, 20);
            more.TextAlign = ContentAlignment.TopRight; body.Controls.Add(more);
            new ToolTip().SetToolTip(more, "A key from another " + label + " account: its own row, On switch, switch order and daily limit.");
            more.Click += (s, e) => ThreadPool.QueueUserWorkItem(_ => { string r = FirestarterApp.Run("add-second " + name).Trim();
                try { BeginInvoke((Action)(() => { if (r != "saved") ThemedBox.Show(this, r, Text); Reload(); })); } catch { } });
        }
        int y2 = line + 24;

        var on = new CheckBox(); on.Text = "On"; on.ForeColor = Ink; on.SetBounds(6, y2 + 2, 50, 22);
        on.Checked = B(p, "on");
        body.Controls.Add(on);

        TextBox key = null;
        if (needsKey)
        {
            key = new TextBox(); key.SetBounds(60, y2, 170, 24); StyleBox(key); key.UseSystemPasswordChar = true;
            body.Controls.Add(key);
            string shown = S(p, "key");
            bool bad = B(p, "key_bad");                 // the provider itself refused it (free-limits)
            var keyState = AddLabel(body, shown == "" ? "no key" : shown + (bad ? "  refused" : S(p, "key_from") == ".env" ? " (.env)" : ""),
                                    234, y2 + 3, 150, shown == "" ? Dim : bad ? Gold : Ok);
            var save = MakeButton(body, "Save key", 388, y2 - 1, 72);
            var k = key;
            save.Click += (s, e) =>
            {
                string v = k.Text.Trim(); k.Text = "";
                if (v == "") return;
                ThreadPool.QueueUserWorkItem(_ => { string r = FirestarterApp.RunWithInput("set-key " + name, v);
                    try { BeginInvoke((Action)(() => { if (r != "saved") ThemedBox.Show(this, r, Text); Reload(); })); } catch { } });
            };
            // a second key's row with no key yet can be taken away too
            if (S(p, "key_from") == "saved" || (S(p, "second_of") != "" && shown == ""))
            {
                var del = MakeButton(body, "Remove", 464, y2 - 1, 62);
                del.Click += (s, e) => ThreadPool.QueueUserWorkItem(_ => { FirestarterApp.Run("del-key " + name);
                    try { BeginInvoke((Action)Reload); } catch { } });
            }
        }
        else AddLabel(body, "no key needed", 60, y2 + 3, 190, Dim);

        var model = new TextBox(); model.SetBounds(530, y2, 112, 24); StyleBox(model); model.Text = S(p, "model");
        Hint(model, "model name");
        body.Controls.Add(model);
        var tip = new ToolTip(); tip.SetToolTip(model, "The model " + label + " runs, as its own site names it. Saved when you press Enter or leave the box.");
        var result = AddLabel(body, "", 330, y, 266, Dim);
        result.TextAlign = ContentAlignment.TopRight; result.AutoEllipsis = true;
        // a long reason (a failed test, a "not ready") doesn't fit on the row: hover shows it, a click shows it
        // whole in a box that can be copied from (Nov 15: the reason for a failed test was cut off)
        string fullResult = "";
        Action<string, Color> showResult = (r, c) => { fullResult = r; result.Text = r; result.ForeColor = c;
            tip.SetToolTip(result, r); result.Cursor = r.Length > 40 ? Cursors.Hand : Cursors.Default; };
        result.Click += (s, e) => { if (fullResult.Length > 40) ThemedBox.Show(this, fullResult, label + " test"); };
        string problem = S(p, "problem");
        if (problem != "") showResult("Not ready: " + problem, Gold);
        else if (B(p, "out")) showResult("Out of free tokens for now (switching skips it)", Dim);
        if (cloud) AddSwitchBox(p, name, label, y + 20, tip);
        string savedModel = model.Text;
        Action<bool> saveModel = reload => { string v = model.Text.Trim().Replace("\"", "");
            if (v == savedModel) return;
            savedModel = v;
            ThreadPool.QueueUserWorkItem(_ => { FirestarterApp.Run("provider-set " + name + " \"model=" + v + "\"");
                if (reload) try { BeginInvoke((Action)Reload); } catch { } }); };
        model.Leave += (s, e) => saveModel(false);   // no redraw here: it would clear a key being typed
        model.KeyDown += (s, e) => { if (e.KeyCode == Keys.Enter) { e.SuppressKeyPress = true; saveModel(true); } };
        var test = MakeButton(body, "Test", 646, y2 - 1, 50);
        test.Click += (s, e) =>
        {
            if (ThemedBox.Show(this, "Send " + label + " one tiny request (“Reply with the single word OK”)? " +
                    "Nothing of yours is sent. A cloud provider may charge a fraction of a cent.", Text,
                    MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
            showResult("Testing…", Dim);
            ThreadPool.QueueUserWorkItem(_ => { string r = FirestarterApp.Run("test-provider " + name).Trim();
                try { BeginInvoke((Action)(() => showResult(r, r.StartsWith("Works") ? Ok : Gold))); } catch { } });
        };
        var keyBox = key;
        string page = S(p, "key_page");
        if (page != "")
        {
            var link = new LinkLabel(); link.Text = needsKey ? "Get a key" : "Website"; link.LinkColor = Gold;
            link.SetBounds(600, y + 2, 96, 20); link.TextAlign = ContentAlignment.TopRight; body.Controls.Add(link);
            link.Click += (s, e) => { try { Process.Start(page); } catch { } };
        }
        on.CheckedChanged += (s, e) =>
        {
            if (filling) return;
            if (on.Checked && needsKey && S(p, "key") == "" && (keyBox == null || keyBox.Text.Trim() == ""))
            {
                ThemedBox.Show(this, "Save a key for " + label + " first.", Text);
                filling = true; on.Checked = false; filling = false; return;
            }
            string extra = "";
            if (on.Checked && trains && !B(p, "accepted"))
            {
                var a = ThemedBox.Show(this, label + "'s free tier may use what's sent to improve its products " +
                    "(training). Allow it for " + label + "?", Text, MessageBoxButtons.YesNo, MessageBoxIcon.Warning);
                if (a != DialogResult.Yes) { filling = true; on.Checked = false; filling = false; return; }
                extra = " accept=yes";
            }
            if (!on.Checked && trains) extra = " accept=no";
            string arg = "provider-set " + name + (on.Checked ? " on" : " off") + extra;
            ThreadPool.QueueUserWorkItem(_ => { FirestarterApp.Run(arg); try { BeginInvoke((Action)Reload); } catch { } });
        };
        return y2 + 40 - y;
    }

    // "Switch order": 1st, 2nd... for the providers to use one after another as free tokens run out
    // (worker_cloud.py Chain; cloud.switch_order). "-" = not in it.
    void AddSwitchBox(Dictionary<string, object> p, string name, string label, int y, ToolTip tip)
    {
        AddLabel(body, "Switch order", 554, y + 1, 84, Dim);
        var box = new ComboBox(); box.SetBounds(640, y - 2, 56, 24);
        box.DropDownStyle = ComboBoxStyle.DropDownList; box.FlatStyle = FlatStyle.Flat;
        box.BackColor = PurpleFill; box.ForeColor = Ink; box.DrawMode = DrawMode.OwnerDrawFixed; box.ItemHeight = 18;
        box.DrawItem += (s, e) =>
        {
            bool sel = (e.State & DrawItemState.Selected) != 0 && (e.State & DrawItemState.ComboBoxEdit) == 0;
            using (var bg = new SolidBrush(sel ? PurpleLine : PurpleFill)) e.Graphics.FillRectangle(bg, e.Bounds);
            if (e.Index >= 0)
                TextRenderer.DrawText(e.Graphics, box.Items[e.Index].ToString(), box.Font,
                    new Rectangle(e.Bounds.X + 4, e.Bounds.Y, e.Bounds.Width - 4, e.Bounds.Height), Ink,
                    TextFormatFlags.VerticalCenter | TextFormatFlags.Left);
        };
        box.Items.Add("-");
        for (int i = 1; i <= 9; i++) box.Items.Add(i.ToString());
        int order = 0; int.TryParse(S(p, "order"), out order);
        box.SelectedIndex = order >= 0 && order <= 9 ? order : 0;
        tip.SetToolTip(box, "Use providers one after another: when " + label + " runs out of free tokens, the job " +
                            "carries on with the next number, and last on this PC's model. \"-\" leaves it out.");
        body.Controls.Add(box);
        box.SelectedIndexChanged += (s, e) =>
        {
            if (filling) return;
            string arg = "provider-set " + name + " order=" + box.SelectedIndex;
            ThreadPool.QueueUserWorkItem(_ => { FirestarterApp.Run(arg); try { BeginInvoke((Action)Reload); } catch { } });
        };
    }

    static string Checked(string ymd)
    {
        DateTime d;
        return DateTime.TryParseExact(ymd, "yyyy-MM-dd", System.Globalization.CultureInfo.InvariantCulture,
                                      System.Globalization.DateTimeStyles.None, out d) ? d.ToString("MMM d") : ymd;
    }

    void SaveLimits()
    {
        double t, u;
        if (!double.TryParse(tokensDay.Text.Trim(), out t) || !double.TryParse(usdDay.Text.Trim(), out u) || t < 0 || u < 0)
        { footer.Text = "Limits must be numbers, 0 or more."; return; }
        string arg = "set-limits " + ((long)t) + " " + u.ToString(System.Globalization.CultureInfo.InvariantCulture);
        ThreadPool.QueueUserWorkItem(_ => { string r = FirestarterApp.Run(arg).Trim();
            try { BeginInvoke((Action)(() => footer.Text = r == "saved" ? "Limits saved." : r)); } catch { } });
    }
}

class FirestarterForm : Form
{
    static readonly Color Bg = Color.FromArgb(2, 7, 5);
    static readonly Color Ink = Color.FromArgb(200, 230, 210);
    static readonly Color Dim = Color.FromArgb(120, 140, 130);
    static readonly Color Ember = FirestarterApp.Accent;
    static readonly Color Gold = Color.FromArgb(231, 195, 104);
    static readonly Color PurpleFill = Color.FromArgb(14, 16, 25);
    static readonly Color PurpleLine = Color.FromArgb(64, 53, 105);

    readonly Label title = new Label(), state = new Label(), task = new Label(), queue = new Label(), game = new Label();
    readonly List<ComboBox> boxes = new List<ComboBox>();
    Button codeChat;
#if SMITHY
    readonly ComboBox provider = new ComboBox();
    readonly List<string> providerNames = new List<string>();
    Label cloudLine;
    Button plansButton;
    bool loadingCloud;
    int cloudTicks;
#endif
#if !SMITHY
    Button siteJoomla, siteWp;
    Label siteSummary;
#endif
    bool checkingPage;
    int pageMisses = 2;
    readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
    Process worker;
    bool loadingModels, stopping;
    string runningTask = "";   // the task the worker is on now ("" = none), for the Close button's question
    string askedOut = "";      // the task already asked about free tokens (worker_cloud.Chain)
    ProgressForm progress;     // the live steps window (one at a time)

    public FirestarterForm()
    {
        Text = FirestarterApp.Name;
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
#if SMITHY
        ClientSize = new Size(420, 176 + 36 * FirestarterApp.Tiers.Length + 128 + 62 + 38 + 38 + 38);
#else
        ClientSize = new Size(420, 176 + 36 * FirestarterApp.Tiers.Length + 116 + 166 + 76);
#endif
        BackColor = Bg; ForeColor = Ink;
        Font = new Font("Segoe UI", 10f);
        AutoAnchor.Fit(this, false);                    // the main window: sizable and can maximize

        title.Text = FirestarterApp.Title;
        title.Font = new Font("Segoe UI Semibold", 14f);
        title.ForeColor = Ember;
        title.SetBounds(20, 14, 380, 30);
        Label sub = new Label();
        sub.Text = FirestarterApp.Sub;
        sub.ForeColor = Dim; sub.SetBounds(22, 42, 380, 20);
        Controls.Add(title); Controls.Add(sub);

        int y = 70;
        foreach (var l in new[] { state, task, queue, game }) { l.SetBounds(22, y, 380, 22); Controls.Add(l); y += 24; }

        int y2 = 176;
        foreach (var t in FirestarterApp.Tiers) { var box = new ComboBox(); boxes.Add(box); AddModelRow(t[0], box, t[1], y2); y2 += 36; }
#if SMITHY
        // which model provider the next tasks use (smithy.json cloud.default; worker_cloud.py).
        // Only providers already turned on (in Providers..., with a key) are offered here.
        AddProviderRow(y2); y2 += 36;
        cloudLine = new Label(); cloudLine.ForeColor = Dim; cloudLine.SetBounds(150, y2, 250, 22);
        Controls.Add(cloudLine); y2 += 26;
        var providersButton = MakeButton("Providers…", 150, y2, 250);
        providersButton.Click += (s, e) => { using (var d = new ProvidersForm()) d.ShowDialog(this); LoadCloud(); };
        y2 += 38;
#endif
#if !SMITHY
        // which kind of website the next tasks are written for (worker.json "site"); Site setup...
        // holds that site's answers and extensions (worker.json "sites", site_profiles.json)
        var siteLabel = new Label(); siteLabel.Text = "Site"; siteLabel.ForeColor = Dim; siteLabel.SetBounds(22, y2 + 6, 120, 22);
        Controls.Add(siteLabel);
        siteJoomla = MakeButton("Joomla", 150, y2, 122);
        siteWp = MakeButton("WordPress", 278, y2, 122);
        siteJoomla.Click += (s, e) => SetSite("joomla");
        siteWp.Click += (s, e) => SetSite("wordpress");
        y2 += 36;
        var setupButton = MakeButton("Site setup\u2026", 150, y2, 250);
        setupButton.Click += (s, e) => OpenSetup();
        y2 += 36;
        siteSummary = new Label();
        siteSummary.ForeColor = Dim; siteSummary.SetBounds(150, y2, 250, 40);
        Controls.Add(siteSummary);
        y2 += 44;
#endif

        var get = MakeButton("Get models…", 150, y2 + 6, 250);
        get.Click += (s, e) => { using (var d = new DownloaderForm()) d.ShowDialog(this); LoadModels(); };
        var logButton = MakeButton("Work log", 150, y2 + 44, 122);
        logButton.Click += (s, e) => OpenWorkLog();
        // each step of the running task, live, in a small window to keep in a corner
        var progressButton = MakeButton("Progress", 278, y2 + 44, 122);
        progressButton.Click += (s, e) =>
        {
            if (progress == null || progress.IsDisposed) { progress = new ProgressForm(); progress.Show(this); }
            else { progress.WindowState = FormWindowState.Normal; progress.Activate(); }
        };
#if SMITHY
        // Othos's code chat: needs Othos's page server (port 3000), so it's off until Othos runs
        codeChat = MakeButton("Code chat", 150, y2 + 82, 250);
        codeChat.Enabled = false;
        codeChat.Click += (s, e) => FirestarterApp.OpenPage("http://127.0.0.1:3000/code");
        // a build's plan, for her yes, no, or answers (PlanForm)
        plansButton = MakeButton("Plans…", 150, y2 + 120, 250);
        plansButton.Click += (s, e) => LoadPlans(true);
        // close The Smithy and its worker, the same as the window's X (Nov 16, the user: there was no Close button)
        var closeButton = MakeButton("Close The Smithy", 150, y2 + 158, 250);
        closeButton.Click += (s, e) =>
        {
            if (runningTask != "" && ThemedBox.Show(this, "Task " + runningTask + " is running. Close The Smithy? " +
                    "The task stops here and goes back in the queue; it picks up where it left off when The Smithy " +
                    "starts again.", Text, MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
                return;
            Close();
        };
#else
        var wpButton = MakeButton("WordPress drafts\u2026", 150, y2 + 82, 250);
        wpButton.Click += (s, e) => { using (var d = new WordPressForm()) d.ShowDialog(this); };
        var zooButton = MakeButton("ZOO items\u2026", 150, y2 + 120, 250);
        zooButton.Click += (s, e) => { using (var d = new ZooForm()) d.ShowDialog(this); };
        var pcButton = MakeButton("Check my PC\u2026", 150, y2 + 158, 250);
        pcButton.Click += (s, e) => CheckPc();
        // other model providers (shared window; worker.json "cloud"; off until one is turned on).
        // No restart needed: the worker reads its settings and keys again at each task.
        var provButton = MakeButton("Providers…", 150, y2 + 196, 250);
        provButton.Click += (s, e) => { using (var d = new ProvidersForm()) d.ShowDialog(this); };
#endif

        timer.Interval = 2000;
        timer.Tick += (s, e) => RefreshStatus();
    }

    Button MakeButton(string text, int x, int y, int w)
    {
        var b = new Button();
        b.Text = text; b.SetBounds(x, y, w, 32);
        b.FlatStyle = FlatStyle.Flat;
        b.FlatAppearance.BorderColor = PurpleLine;
        b.FlatAppearance.MouseOverBackColor = Color.FromArgb(25, 24, 43);
        b.BackColor = PurpleFill; b.ForeColor = Ink;
        Controls.Add(b);
        return b;
    }

    // workers\<Name>.md, rewritten from the queue first so it's current
    void OpenWorkLog()
    {
        ThreadPool.QueueUserWorkItem(_ =>
        {
            FirestarterApp.Run("log");
            string path = Path.Combine(FirestarterApp.AgentDir, "workers", FirestarterApp.WorkLog);
            string npp = @"C:\Program Files\Notepad++\notepad++.exe";
            try { Process.Start(File.Exists(npp) ? npp : "explorer.exe", "\"" + path + "\""); } catch { }
        });
    }

    void AddModelRow(string label, ComboBox box, string tier, int y)
    {
        var l = new Label(); l.Text = label; l.ForeColor = Dim; l.SetBounds(22, y + 4, 120, 22); Controls.Add(l);
        box.SetBounds(150, y, 250, 28);
        box.DropDownStyle = ComboBoxStyle.DropDownList;
        box.FlatStyle = FlatStyle.Flat;
        box.BackColor = PurpleFill; box.ForeColor = Ink;
        // drawn by hand: Windows ignores BackColor on this kind of drop-down and paints it white
        box.DrawMode = DrawMode.OwnerDrawFixed;
        box.ItemHeight = 22;
        box.DrawItem += (s, e) =>
        {
            bool sel = (e.State & DrawItemState.Selected) != 0 && (e.State & DrawItemState.ComboBoxEdit) == 0;
            using (var bg = new SolidBrush(sel ? PurpleLine : PurpleFill)) e.Graphics.FillRectangle(bg, e.Bounds);
            if (e.Index >= 0)
                TextRenderer.DrawText(e.Graphics, box.Items[e.Index].ToString(), box.Font,
                    new Rectangle(e.Bounds.X + 4, e.Bounds.Y, e.Bounds.Width - 4, e.Bounds.Height), Ink,
                    TextFormatFlags.VerticalCenter | TextFormatFlags.Left);
        };
        box.SelectedIndexChanged += (s, e) =>
        {
            if (loadingModels || box.SelectedItem == null) return;
            string name = box.SelectedItem.ToString();
            ThreadPool.QueueUserWorkItem(_ => FirestarterApp.Run("set-model " + tier + " \"" + name + "\""));
        };
        Controls.Add(box);
    }

#if SMITHY
    void AddProviderRow(int y)
    {
        var l = new Label(); l.Text = "Provider"; l.ForeColor = Dim; l.SetBounds(22, y + 4, 120, 22); Controls.Add(l);
        provider.SetBounds(150, y, 250, 28);
        provider.DropDownStyle = ComboBoxStyle.DropDownList;
        provider.FlatStyle = FlatStyle.Flat;
        provider.BackColor = PurpleFill; provider.ForeColor = Ink;
        provider.DrawMode = DrawMode.OwnerDrawFixed;       // as the model row: Windows paints it white otherwise
        provider.ItemHeight = 22;
        provider.DrawItem += (s, e) =>
        {
            bool sel = (e.State & DrawItemState.Selected) != 0 && (e.State & DrawItemState.ComboBoxEdit) == 0;
            using (var bg = new SolidBrush(sel ? PurpleLine : PurpleFill)) e.Graphics.FillRectangle(bg, e.Bounds);
            if (e.Index >= 0)
                TextRenderer.DrawText(e.Graphics, provider.Items[e.Index].ToString(), provider.Font,
                    new Rectangle(e.Bounds.X + 4, e.Bounds.Y, e.Bounds.Width - 4, e.Bounds.Height), Ink,
                    TextFormatFlags.VerticalCenter | TextFormatFlags.Left);
        };
        provider.SelectedIndexChanged += (s, e) =>
        {
            if (loadingCloud || provider.SelectedIndex < 0) return;
            string name = providerNames[provider.SelectedIndex];
            ThreadPool.QueueUserWorkItem(_ => { FirestarterApp.Run("set-provider " + name); LoadCloud(); });
        };
        Controls.Add(provider);
    }

    // a build's plan waiting for her answer (smithy_jobs.run_proposal): opened by itself once per plan,
    // or from the Plans button. Checked every 30 seconds, with the cloud line.
    readonly HashSet<string> shownPlans = new HashSet<string>();
    bool loadingPlans;

    void LoadPlans(bool openAll)
    {
        if (loadingPlans) return;
        loadingPlans = true;
        ThreadPool.QueueUserWorkItem(_ =>
        {
            System.Collections.ArrayList list = null;
            try { list = new JavaScriptSerializer().Deserialize<System.Collections.ArrayList>(FirestarterApp.Run("plans")); } catch { }
            try { BeginInvoke((Action)(() => { loadingPlans = false; ShowPlans(list, openAll); })); } catch { loadingPlans = false; }
        });
    }

    void ShowPlans(System.Collections.ArrayList list, bool openAll)
    {
        int n = list != null ? list.Count : 0;
        plansButton.Text = n > 0 ? "Plans waiting for you (" + n + ")…" : "Plans…";
        plansButton.FlatAppearance.BorderColor = n > 0 ? Ember : PurpleLine;
        if (list == null || n == 0)
        {
            if (openAll) ThemedBox.Show(this, "No plan is waiting for your answer.", FirestarterApp.Name);
            return;
        }
        foreach (Dictionary<string, object> p in list)
        {
            string id = Get(p, "id");
            if (!openAll && shownPlans.Contains(id)) continue;
            shownPlans.Add(id);
            if (Application.OpenForms["plan-" + id] != null) { Application.OpenForms["plan-" + id].Activate(); continue; }
            var f = new PlanForm(id, Get(p, "title"), !openAll);   // opened by itself: no focus taken, it flashes
            f.FormClosed += (s, e) => LoadPlans(false);
            // its own taskbar button (an owned window has none, and hides while The Smithy is minimized)
            f.Show();
            if (!openAll) break;                       // one at a time when it opens by itself
        }
    }

    // what's turned on, the default, and today's cloud use (smithy.py cloud)
    void LoadCloud()
    {
        ThreadPool.QueueUserWorkItem(_ =>
        {
            Dictionary<string, object> m = null;
            try { m = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(FirestarterApp.Run("cloud")); } catch { }
            try { BeginInvoke((Action)(() => FillCloud(m))); } catch { }
        });
    }

    void FillCloud(Dictionary<string, object> m)
    {
        loadingCloud = true;
        provider.Items.Clear(); providerNames.Clear();
        provider.Items.Add("Local (this PC)"); providerNames.Add("local");
        var ready = m != null && m.ContainsKey("ready") ? m["ready"] as System.Collections.ArrayList : null;
        if (ready != null)
            foreach (Dictionary<string, object> p in ready) { provider.Items.Add(Get(p, "label")); providerNames.Add(Get(p, "name")); }
        int i = providerNames.IndexOf(Get(m, "default"));
        provider.SelectedIndex = i >= 0 ? i : 0;
        loadingCloud = false;
        var today = m != null && m.ContainsKey("today") ? m["today"] as Dictionary<string, object> : null;
        long tokens = today != null ? Convert.ToInt64(today["tokens"]) : 0;
        double usd = today != null ? Convert.ToDouble(today["usd"]) : 0;
        if (m == null) cloudLine.Text = "Cloud: couldn't read the settings";
        else if (ready == null || ready.Count == 0) cloudLine.Text = "Cloud providers: off (see smithy.json)";
        else cloudLine.Text = "Cloud today: " + tokens.ToString("N0") + " tokens" + (usd > 0 ? ", $" + usd.ToString("0.00") : "");
        cloudLine.ForeColor = provider.SelectedIndex > 0 ? Gold : Dim;
    }

    protected override void OnActivated(EventArgs e)
    {
        base.OnActivated(e);
        LoadCloud();
    }
#endif

    protected override void OnShown(EventArgs e)
    {
        base.OnShown(e);
        StartWorker();
        LoadModels();
#if SMITHY
        LoadCloud();
#endif
#if !SMITHY
        LoadSite();
#endif
        RefreshStatus();
        timer.Start();
#if !SMITHY
        // asked once (here or in Othos): the file holds the specs, or the "No"
        if (!File.Exists(Path.Combine(FirestarterApp.AgentDir, @"state\system-specs.json"))) AskSpecs();
#endif
        WindowState = FormWindowState.Minimized;   // lives on the taskbar
    }

#if !SMITHY
    void AskSpecs()
    {
        var a = ThemedBox.Show(this,
            "May I check your current system specs, so I can best advise you of settings?\n\n" +
            "This reads your PC's hardware only. Nothing leaves this computer.\n\n" +
            "(No: you won't be asked again. Check my PC... does it any time.)",
            "Firestarter: first start", MessageBoxButtons.YesNo, MessageBoxIcon.Question);
        if (a == DialogResult.Yes) CheckPc();
        // only a real No is remembered; closing the box (X, Alt+F4) asks again next start
        else if (a == DialogResult.No) ThreadPool.QueueUserWorkItem(_ => FirestarterApp.Run("specs-decline"));
    }

    void CheckPc()
    {
        bool applied;
        using (var d = new SpecsForm()) { d.ShowDialog(this); applied = d.Applied; }
        if (applied) RestartWorker();
    }

    // worker.json is read when the worker starts: start it again (a running task goes back in the queue)
    void RestartWorker()
    {
        try
        {
            if (worker != null && !worker.HasExited)
            {
                var k = new ProcessStartInfo("taskkill", "/PID " + worker.Id + " /T /F");
                k.CreateNoWindow = true; k.UseShellExecute = false;
                Process.Start(k).WaitForExit(15000);
            }
        }
        catch { }
        StartWorker();
        LoadModels();
    }
#endif

    void StartWorker()
    {
        FirestarterApp.SaveOldLog();
        Directory.CreateDirectory(FirestarterApp.LogDir);
        var psi = new ProcessStartInfo("cmd.exe",
            "/s /c \"\"" + FirestarterApp.Py + "\" -u " + FirestarterApp.Script + " run > \"" +
            Path.Combine(FirestarterApp.LogDir, FirestarterApp.LogName + ".log") + "\" 2>&1\"");
        psi.WorkingDirectory = FirestarterApp.AgentDir;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        worker = Process.Start(psi);
    }

    void LoadModels()
    {
        ThreadPool.QueueUserWorkItem(_ =>
        {
            Dictionary<string, object> m = null;
            try { m = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(FirestarterApp.Run("models")); } catch { }
            try { BeginInvoke((Action)(() => FillModels(m))); } catch { }
        });
    }

    void FillModels(Dictionary<string, object> m)
    {
        loadingModels = true;
        for (int i = 0; i < boxes.Count; i++)
        {
            var box = boxes[i];
            string tier = FirestarterApp.Tiers[i][1];
            box.Items.Clear();
            string current = m != null && m.ContainsKey(tier) ? Convert.ToString(m[tier]) : "";
            if (m != null && m.ContainsKey("available"))
                foreach (var name in (System.Collections.ArrayList)m["available"]) box.Items.Add(Convert.ToString(name));
            if (current != "" && !box.Items.Contains(current)) box.Items.Insert(0, current);
            box.SelectedItem = current;
        }
        loadingModels = false;
    }

#if !SMITHY
    void LoadSite()
    {
        ThreadPool.QueueUserWorkItem(_ =>
        {
            Dictionary<string, object> m = null;
            try { m = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(FirestarterApp.Run("site")); } catch { }
            if (m == null) return;
            string site = Get(m, "site"), summary = Get(m, "summary");
            try { BeginInvoke((Action)(() => ShowSite(site, summary))); } catch { }
        });
    }

    // re-read it when you come back to the window (it may have changed from the command line)
    protected override void OnActivated(EventArgs e)
    {
        base.OnActivated(e);
        if (siteJoomla != null) LoadSite();
    }

    string currentSite = "joomla";

    void SetSite(string site)
    {
        ShowSite(site, "");
        ThreadPool.QueueUserWorkItem(_ => { FirestarterApp.Run("set-site " + site); LoadSite(); });
    }

    void OpenSetup()
    {
        using (var d = new SiteSetupForm(currentSite)) d.ShowDialog(this);
        LoadSite();
    }

    // the chosen site is lit, with a one-line summary of its setup under Site setup...
    void ShowSite(string site, string summary)
    {
        bool wp = site == "wordpress";
        currentSite = wp ? "wordpress" : "joomla";
        foreach (var b in new[] { siteJoomla, siteWp })
        {
            bool on = b == (wp ? siteWp : siteJoomla);
            b.BackColor = on ? PurpleLine : PurpleFill;
            b.ForeColor = on ? Ember : Ink;
            b.Font = new Font(Font, on ? FontStyle.Bold : FontStyle.Regular);
        }
        if (summary != "") siteSummary.Text = summary;
    }
#endif

    // every provider in the switch order is out of free tokens: her choice, asked once per task
    void AskOut(string tid, string title, string back)
    {
        var a = ThemedBox.Show(this, "Task " + tid + (title != "" ? " (" + title + ")" : "") + " has used up the free tokens " +
            "on every provider in the switch order. It's waiting, and nothing is lost.\n\n" +
            "Yes: finish it on this PC's model now (slower; nothing more is sent out).\n" +
            "No: wait, and it carries on by itself" + (back != "" ? " about " + back : "") + " when free tokens are back.",
            FirestarterApp.Name, MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button1,
            () => OutAnswered(tid));
        if (a == DialogResult.Cancel) return;           // answered by voice meanwhile: that answer stands
        string choice = a == DialogResult.Yes ? "local" : "wait";
        ThreadPool.QueueUserWorkItem(_ => FirestarterApp.Run("out-choice " + tid + " " + choice));
    }

    // the free-tokens question was answered somewhere else (voice), or the task isn't waiting any more
    static bool OutAnswered(string tid)
    {
        var s = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(FirestarterApp.StatusFile));
        string st = s.ContainsKey("state") ? Convert.ToString(s["state"]) : "";
        string now = s.ContainsKey("task_id") ? Convert.ToString(s["task_id"]) : "";
        string choice = s.ContainsKey("out_choice") ? Convert.ToString(s["out_choice"]) : "";
        return st != "waiting for free tokens" || now != tid || choice != "";
    }

    static string Clock(string ts)
    {
        double t;
        if (!double.TryParse(ts, out t)) return "";   // Get() wrote it in this PC's own number format
        var when = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc).AddSeconds(t).ToLocalTime();
        return when.Date == DateTime.Today ? when.ToString("h:mm tt") : when.ToString("ddd h:mm tt");
    }

    void RefreshStatus()
    {
        Dictionary<string, object> s = null;
        try { s = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(FirestarterApp.StatusFile)); } catch { }
        double age = 999;
        if (s != null && s.ContainsKey("updated_ts"))
            age = (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds - Convert.ToDouble(s["updated_ts"]);
        bool alive = worker != null && !worker.HasExited && age < 20;
        string st = s != null && s.ContainsKey("state") ? Convert.ToString(s["state"]) : "";
        if (!alive)
        {
            state.Text = "\u25CB  Worker " + (worker != null && worker.HasExited ? "stopped: see logs\\" + FirestarterApp.LogName + ".log" : "starting\u2026");
            state.ForeColor = Gold;
        }
        else
        {
            string other = Get(s, "waiting_for_cpu");
            state.Text = "\u25CF  Worker " + st + (st == "waiting for the CPU" && other != "" ? " (" + other + " has it)"
                                                    : " (" + Get(s, "backend") + ", CPU only)");
            state.ForeColor = st == "paused" || st == "held back" || st == "waiting for the CPU" ||
                              st == "waiting for free tokens" ? Gold : Ember;
        }
        string tid = Get(s, "task_id");
        runningTask = alive ? tid : "";
        string pct = Get(s, "progress") != "" ? " " + (int)(Convert.ToDouble(Get(s, "progress")) * 100) + "%" : "";
        string el = Get(s, "elapsed") != "" ? ", " + Get(s, "elapsed") + " s" : "";
        var paused = s != null && s.ContainsKey("paused_task") ? s["paused_task"] as Dictionary<string, object> : null;
        task.Text = alive && tid != "" ? "Task " + tid + ": " + (Get(s, "task_title") != "" ? Get(s, "task_title") : Get(s, "task_type")) +
                                         pct + el + " (" + Get(s, "model") + ", " + Get(s, "threads") + " threads)"
                  : alive && paused != null ? "Task " + Get(paused, "id") + " paused: " + Get(paused, "title")
                  : "No task running";
        if (alive && st == "waiting for free tokens" && tid != "")
        {
            string back = Clock(Get(s, "free_tokens_back_ts"));
            task.Text = "Task " + tid + " is waiting for free tokens" + (back != "" ? " (carries on about " + back + ")" : "");
            if (askedOut != tid && Get(s, "out_choice") == "")
            {
                askedOut = tid;
                string title = Get(s, "task_title");
                BeginInvoke((Action)(() => AskOut(tid, title, back)));
            }
        }
        queue.Text = "Queued: " + (alive ? Get(s, "queued") : "-") + "     Waiting for review: " + (alive ? Get(s, "review") : "-");
        string g = Get(s, "game");
        game.Text = alive && g != "" ? "Game: " + g : "No game running";
        game.ForeColor = alive && g != "" ? Gold : Dim;
        Text = alive ? FirestarterApp.Name + ": " + st : FirestarterApp.Name;
#if SMITHY
        if (++cloudTicks % 15 == 0) LoadCloud();        // today's cloud use, every 30 seconds
        if (alive && cloudTicks % 15 == 2) LoadPlans(false);   // a plan waiting for her answer
#endif
        if (codeChat != null && !checkingPage)
        {
            checkingPage = true;
            ThreadPool.QueueUserWorkItem(_ =>
            {
                bool up = FirestarterApp.PortUp(3000);
                try { BeginInvoke((Action)(() => {
                    // off only after two misses in a row: the dev server is sometimes slow to answer
                    pageMisses = up ? 0 : pageMisses + 1;
                    bool on = pageMisses < 2;
                    codeChat.Enabled = on;
                    codeChat.Text = on ? "Code chat" : "Code chat (start Othos first)";
                    checkingPage = false;
                })); } catch { checkingPage = false; }
            });
        }
    }

    static string Get(Dictionary<string, object> s, string k)
    {
        return s != null && s.ContainsKey(k) && s[k] != null ? Convert.ToString(s[k]) : "";
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        if (!stopping)
        {
            stopping = true;
            timer.Stop();
            Text = FirestarterApp.Name + ": stopping\u2026";
            Application.DoEvents();
            try
            {
                // stop the worker and anything it started (a running task goes back in the queue next time)
                if (worker != null && !worker.HasExited)
                {
                    var k = new ProcessStartInfo("taskkill", "/PID " + worker.Id + " /T /F");
                    k.CreateNoWindow = true; k.UseShellExecute = false;
                    Process.Start(k).WaitForExit(15000);
                }
            }
            catch { }
        }
        base.OnFormClosing(e);
    }
}

#if SMITHY
// "Plan" window: a build's plan waiting for her answer (smithy_jobs.run_proposal; the user, Nov 14: "read
// a prompt, ask for clarification where needed, create a plan offered with a yes/no prompt and make any
// adaptations for further user input"). Build it = yes; Send answers = a revised plan; Don't build = no
// (asked again first). Closing it (X, Escape, Alt+F4) answers nothing: the plan keeps waiting.
class PlanForm : Form
{
    static readonly Color Bg = Color.FromArgb(2, 7, 5), Ink = Color.FromArgb(200, 230, 210),
        Dim = Color.FromArgb(120, 140, 130), Field = Color.FromArgb(10, 14, 20),
        PurpleFill = Color.FromArgb(14, 16, 25), PurpleLine = Color.FromArgb(64, 53, 105),
        PurpleHover = Color.FromArgb(25, 24, 43);
    readonly string id;
    readonly bool quiet;                                // opened by itself: no focus taken, the taskbar flashes
    readonly TextBox plan = new TextBox(), other = new TextBox();
    readonly Panel answers = new Panel();
    readonly List<TextBox> boxes = new List<TextBox>();
    readonly List<string> asked = new List<string>(), defaults = new List<string>();
    readonly Button build, buildWith, send, drop, later;
    readonly Label note = new Label();
    readonly System.Windows.Forms.Timer watch = new System.Windows.Forms.Timer();
    bool busy = true, checking, answered;

    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    struct FLASHWINFO { public uint cbSize; public IntPtr hwnd; public uint dwFlags; public uint uCount; public uint dwTimeout; }
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool FlashWindowEx(ref FLASHWINFO f);

    protected override bool ShowWithoutActivation { get { return quiet; } }

    public PlanForm(string id, string title, bool quiet)
    {
        this.id = id; this.quiet = quiet;
        Name = "plan-" + id;
        Text = FirestarterApp.Name + ": plan for task " + id;
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        BackColor = Bg; ForeColor = Ink; Font = new Font("Segoe UI", 10f);
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(720, 680);                // the opening size, before the controls (WindowFit)

        var stripe = new Panel(); stripe.BackColor = FirestarterApp.Accent; stripe.SetBounds(0, 0, 6, 680);
        stripe.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left;
        var head = new Label(); head.Text = "Task " + id + ": " + (title != "" ? title : "a build") + "\nNothing has been built yet.";
        head.ForeColor = FirestarterApp.Accent; head.SetBounds(24, 12, 676, 44);
        head.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;

        plan.Multiline = true; plan.ReadOnly = true; plan.ScrollBars = ScrollBars.Vertical; plan.WordWrap = true;
        plan.BackColor = Field; plan.ForeColor = Ink; plan.BorderStyle = BorderStyle.FixedSingle;
        plan.Text = "Loading the plan…";
        plan.SetBounds(24, 60, 676, 300);
        plan.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;

        // one box per question, filled in with what it will do if not told; then any other changes
        var ask = new Label(); ask.Text = "Your answers (each is filled in with what The Smithy will do if you leave it):";
        ask.ForeColor = Dim; ask.SetBounds(24, 368, 676, 22);
        ask.Anchor = AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
        answers.AutoScroll = true; answers.BackColor = Bg; answers.SetBounds(24, 392, 676, 168);
        answers.Anchor = AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
        other.Multiline = true; other.AcceptsReturn = true; other.ScrollBars = ScrollBars.Vertical;
        other.BackColor = Field; other.ForeColor = Ink; other.BorderStyle = BorderStyle.FixedSingle;
        other.TextChanged += (s, e) => Enable();

        note.ForeColor = Dim; note.SetBounds(24, 650, 676, 24);
        note.Anchor = AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
        build = MakeButton("Build it", 24, 104, true);
        buildWith = MakeButton("Build with these answers", 134, 196, false);
        send = MakeButton("Send for a new plan", 336, 160, false);
        drop = MakeButton("Don't build", 502, 100, false);
        later = MakeButton("Later", 608, 92, false);
        later.Anchor = AnchorStyles.Bottom | AnchorStyles.Right;
        build.Click += (s, e) => Answer("yes", false, "Building it…");
        buildWith.Click += (s, e) => Answer(Answers(), true, "Building it with your answers…");
        send.Click += (s, e) => Answer(Answers(), false, "Sending your answers for a new plan…");
        drop.Click += (s, e) =>
        {
            if (ThemedBox.Show(this, "Drop this plan? Task " + id + " won't be built.", FirestarterApp.Name,
                               MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2) == DialogResult.Yes)
                Answer("no", false, "Dropping it…");
        };
        later.Click += (s, e) => Close();
        CancelButton = later;                            // Escape / X / Alt+F4: nothing answered, the plan keeps waiting
        foreach (Control c in new Control[] { head, plan, ask, answers, build, buildWith, send, drop, later, note, stripe }) Controls.Add(c);
        stripe.SendToBack();
        Enable();
        WindowFit.ApplyDialog(this, new Size(640, 520));
        Load += (s, e) => LoadPlan();
        Shown += (s, e) => { if (quiet) Flash(); };
        // answered by voice (or in another window) meanwhile: say so, and nothing here can answer it twice
        // (Nov 14, suggestion 2). Checked every 10 seconds; her typing is left as it is.
        watch.Interval = 10000;
        watch.Tick += (s, e) => CheckStillWaiting();
        Shown += (s, e) => watch.Start();
        FormClosed += (s, e) => watch.Dispose();
        Resize += (s, e) => LayOutAnswers();
    }

    void Flash()
    {
        try
        {
            var f = new FLASHWINFO();
            f.cbSize = (uint)System.Runtime.InteropServices.Marshal.SizeOf(typeof(FLASHWINFO));
            f.hwnd = Handle; f.dwFlags = 3 | 12;         // the caption and the taskbar button, until it's opened
            FlashWindowEx(ref f);
        }
        catch { }
    }

    Button MakeButton(string text, int x, int w, bool main)
    {
        var b = new Button(); b.Text = text; b.SetBounds(x, 610, w, 34);
        b.FlatStyle = FlatStyle.Flat; b.BackColor = PurpleFill; b.ForeColor = Ink;
        b.FlatAppearance.BorderColor = main ? FirestarterApp.Accent : PurpleLine;
        b.FlatAppearance.MouseOverBackColor = PurpleHover;
        b.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
        return b;
    }

    // what she changed, as one reply: each question whose box isn't its default, then the other changes
    string Answers()
    {
        var parts = new List<string>();
        for (int i = 0; i < boxes.Count; i++)
        {
            string a = boxes[i].Text.Trim();
            if (a != defaults[i].Trim()) parts.Add("Question " + (i + 1) + " (" + asked[i] + "): " + (a != "" ? a : "no answer"));
        }
        if (other.Text.Trim() != "") parts.Add("Other changes: " + other.Text.Trim());
        return string.Join("\n", parts.ToArray());
    }

    // Build it only with nothing changed (so answers are never lost); the answer buttons only with something changed
    void Enable()
    {
        bool changed = Answers() != "";
        build.Enabled = !busy && !changed; buildWith.Enabled = !busy && changed; send.Enabled = !busy && changed;
        drop.Enabled = !busy;
        foreach (var b in boxes) b.ReadOnly = busy;
        other.ReadOnly = busy;
        AcceptButton = changed ? null : build;           // Enter presses only the default button
    }

    void FillAnswers(System.Collections.ArrayList qs)
    {
        answers.Controls.Clear(); boxes.Clear(); asked.Clear(); defaults.Clear();
        if (qs != null)
            foreach (Dictionary<string, object> q in qs)
            {
                asked.Add(Convert.ToString(q["q"])); defaults.Add(q.ContainsKey("default") ? Convert.ToString(q["default"]) : "");
                var l = new Label(); l.Text = (asked.Count) + ". " + asked[asked.Count - 1]; l.ForeColor = Ink; l.AutoSize = true;
                var b = new TextBox(); b.Text = defaults[defaults.Count - 1]; b.BackColor = Field; b.ForeColor = Ink;
                b.BorderStyle = BorderStyle.FixedSingle;
                b.TextChanged += (s, e) => Enable();
                answers.Controls.Add(l); answers.Controls.Add(b); boxes.Add(b);
            }
        var ol = new Label(); ol.Text = boxes.Count > 0 ? "Other changes (optional):" : "Your answers or changes (leave empty to build it as it is):";
        ol.ForeColor = Dim; ol.AutoSize = true; ol.Name = "otherLabel";
        answers.Controls.Add(ol); answers.Controls.Add(other);
        LayOutAnswers();
    }

    // the rows follow the window's width (worked out again on every resize, never once at start-up)
    void LayOutAnswers()
    {
        int w = Math.Max(200, answers.ClientSize.Width - SystemInformation.VerticalScrollBarWidth - 4), y = 0;
        answers.AutoScrollPosition = new Point(0, 0);
        foreach (Control c in answers.Controls)
        {
            if (c is Label) { ((Label)c).MaximumSize = new Size(w, 0); c.Location = new Point(0, y); y += c.GetPreferredSize(new Size(w, 0)).Height + 2; }
            else if (c == other) { c.SetBounds(0, y, w, 64); y += 70; }
            else { c.SetBounds(0, y, w, 26); y += 32; }
        }
    }

    void LoadPlan()
    {
        ThreadPool.QueueUserWorkItem(_ =>
        {
            Dictionary<string, object> m = null;
            try { m = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(FirestarterApp.Run("plan " + id)); } catch { }
            try { BeginInvoke((Action)(() => {
                if (m == null) { plan.Text = "The plan couldn't be read."; FillAnswers(null); return; }
                plan.Text = Convert.ToString(m["text"]).Replace("\r\n", "\n").Replace("\n", "\r\n");
                plan.SelectionStart = 0; plan.SelectionLength = 0;
                FillAnswers(m.ContainsKey("questions") ? m["questions"] as System.Collections.ArrayList : null);
                bool waiting = m["waiting"] is bool && (bool)m["waiting"];
                busy = !waiting; Enable();
                note.Text = waiting ? Convert.ToString(m.ContainsKey("cost") ? m["cost"] : "") : "This plan was already answered.";
            })); } catch { }
        });
    }

    void CheckStillWaiting()
    {
        if (checking || answered || (busy && note.Text != "")) return;   // not while its own answer is being sent
        checking = true;
        ThreadPool.QueueUserWorkItem(_ =>
        {
            Dictionary<string, object> m = null;
            try { m = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(FirestarterApp.Run("plan " + id)); } catch { }
            try { BeginInvoke((Action)(() => {
                checking = false;
                if (m == null || (m["waiting"] is bool && (bool)m["waiting"])) return;
                answered = true; watch.Stop(); busy = true; Enable();
                note.ForeColor = Color.FromArgb(231, 195, 104);   // gold, as the main window uses
                note.Text = "This plan was answered somewhere else (by voice, or in another window), so there's nothing more to do here.";
            })); } catch { checking = false; }
        });
    }

    void Answer(string text, bool buildNow, string doing)
    {
        busy = true; Enable(); note.Text = doing;
        ThreadPool.QueueUserWorkItem(_ =>
        {
            string o;
            try { o = FirestarterApp.RunWithInput("reply " + id + (buildNow ? " --build" : "") + " -", text); }
            catch (Exception ex) { o = "can't do that: " + ex.Message; }
            try { BeginInvoke((Action)(() => {
                bool failed = o.StartsWith("can't") || o.Contains("Traceback") || o == "";
                ThemedBox.Show(this, o == "" ? "No answer came back from The Smithy." : o, FirestarterApp.Name,
                               MessageBoxButtons.OK, failed ? MessageBoxIcon.Warning : MessageBoxIcon.None);
                if (failed) { busy = false; Enable(); note.Text = ""; } else Close();
            })); } catch { }
        });
    }
}
#endif

// "Progress" window: each step of the running task as it happens (Nov 13, the user: "a running step by
// step as it progresses thru the task", to keep in a corner). Reads state\<worker>-status-steps.jsonl,
// which the worker writes (worker_engine.StepFile); Othos's Task Mode reads the same lines aloud.
// Only reads files; it starts and stops nothing.
class ProgressForm : Form
{
    static readonly Color Bg = Color.FromArgb(2, 7, 5), Ink = Color.FromArgb(200, 230, 210),
        Dim = Color.FromArgb(120, 140, 130), Accent = FirestarterApp.Accent, Gold = Color.FromArgb(231, 195, 104),
        PurpleFill = Color.FromArgb(14, 16, 25), PurpleLine = Color.FromArgb(64, 53, 105), Panel = Color.FromArgb(5, 12, 8);
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    static readonly string StepsFile = Path.Combine(Path.GetDirectoryName(FirestarterApp.StatusFile),
        Path.GetFileNameWithoutExtension(FirestarterApp.StatusFile) + "-steps.jsonl");

    readonly Label task = new Label();
    readonly Panel bar = new Panel();
    readonly RichTextBox lines = new RichTextBox();
    readonly CheckBox onTop = new CheckBox();
    readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
    double frac;
    long pos;
    string lastTask = "";

    public ProgressForm()
    {
        Text = FirestarterApp.Name + ": progress";
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        FormBorderStyle = FormBorderStyle.Sizable; MaximizeBox = false; MinimizeBox = true;
        BackColor = Bg; ForeColor = Ink; Font = new Font("Segoe UI", 9.5f);
        ClientSize = new Size(440, 340); MinimumSize = new Size(320, 220);
        ShowInTaskbar = false; TopMost = true;
        // the bottom-right corner of the screen, out of the way
        StartPosition = FormStartPosition.Manual;
        Rectangle wa = Screen.PrimaryScreen.WorkingArea;
        Location = new Point(wa.Right - Width - 12, wa.Bottom - Height - 12);

        var title = new Label(); title.Text = "P R O G R E S S"; title.ForeColor = Accent;
        title.Font = new Font("Segoe UI Semibold", 11f); title.SetBounds(12, 8, 300, 22);
        task.ForeColor = Ink; task.SetBounds(12, 32, 416, 20); task.AutoEllipsis = true;
        task.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
        bar.SetBounds(12, 56, 416, 8); bar.BackColor = PurpleFill;
        bar.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
        bar.Paint += (s, e) =>
        {
            int w = (int)(bar.Width * Math.Max(0, Math.Min(1, frac)));
            using (var b = new SolidBrush(Accent)) e.Graphics.FillRectangle(b, 0, 0, w, bar.Height);
        };
        lines.SetBounds(12, 72, 416, 228); lines.ReadOnly = true; lines.BorderStyle = BorderStyle.None;
        lines.BackColor = Panel; lines.ForeColor = Ink; lines.DetectUrls = false; lines.WordWrap = true;
        lines.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
        onTop.Text = "Keep on top"; onTop.Checked = true; onTop.ForeColor = Dim; onTop.FlatStyle = FlatStyle.Flat;
        onTop.SetBounds(12, 308, 140, 24); onTop.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
        onTop.CheckedChanged += (s, e) => TopMost = onTop.Checked;
        var close = new Button(); close.Text = "Close"; close.FlatStyle = FlatStyle.Flat;
        close.FlatAppearance.BorderColor = PurpleLine; close.FlatAppearance.MouseOverBackColor = Color.FromArgb(25, 24, 43);
        close.BackColor = PurpleFill; close.ForeColor = Ink; close.SetBounds(338, 306, 90, 28);
        close.Anchor = AnchorStyles.Bottom | AnchorStyles.Right;
        close.Click += (s, e) => Close();
        CancelButton = close;
        Controls.AddRange(new Control[] { title, task, bar, lines, onTop, close });

        LoadRecent();
        timer.Interval = 1000;
        timer.Tick += (s, e) => Tick();
        timer.Start();
        Tick();
    }

    protected override void OnFormClosed(FormClosedEventArgs e) { timer.Stop(); base.OnFormClosed(e); }

    static string Get(Dictionary<string, object> s, string k)
    {
        return s != null && s.ContainsKey(k) && s[k] != null ? Convert.ToString(s[k]) : "";
    }

    static string ReadShared(string path, long from, out long end)
    {
        using (var f = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
        {
            end = f.Length;
            if (from > end) from = end;
            f.Seek(from, SeekOrigin.Begin);
            var buf = new byte[end - from];
            int n = 0;
            while (n < buf.Length) { int r = f.Read(buf, n, buf.Length - n); if (r <= 0) break; n += r; }
            int keep = Array.LastIndexOf(buf, (byte)'\n') + 1;   // whole lines only
            end = from + keep;
            return System.Text.Encoding.UTF8.GetString(buf, 0, keep);
        }
    }

    // the last 40 steps already there, so it isn't empty when opened part way through a task
    void LoadRecent()
    {
        try
        {
            string all = ReadShared(StepsFile, 0, out pos);
            string[] ls = all.Split(new[] { '\n' }, StringSplitOptions.RemoveEmptyEntries);
            for (int i = Math.Max(0, ls.Length - 40); i < ls.Length; i++) AddLine(ls[i]);
        }
        catch { AddText(DateTime.Now.ToString("HH:mm:ss"), "No steps yet. They appear here as a task runs.", Dim); }
    }

    void AddLine(string json)
    {
        Dictionary<string, object> d;
        try { d = Json.Deserialize<Dictionary<string, object>>(json); } catch { return; }
        string t = Get(d, "task"), text = Get(d, "text");
        if (t != "" && t != lastTask)
        {
            if (lastTask != "") AddText("", "", Dim);
            lastTask = t;
        }
        string when = "";
        try { when = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc).AddSeconds(Convert.ToDouble(d["ts"])).ToLocalTime().ToString("h:mm:ss"); } catch { }
        string low = text.ToLowerInvariant();
        Color c = low.StartsWith("started task") || low.Contains(" is done") ? Accent
                : low.Contains("wait") || low.Contains("slow down") || low.Contains("couldn't") || low.Contains("failed")
                  || low.Contains("cut off") || low.Contains("left out") || low.Contains("can't") || low.Contains("cancelled") ? Gold
                : Ink;
        AddText(when, text, c);
    }

    void AddText(string when, string text, Color c)
    {
        lines.SelectionStart = lines.TextLength; lines.SelectionLength = 0;
        if (when != "") { lines.SelectionColor = Dim; lines.AppendText(when + "  "); }
        lines.SelectionColor = c; lines.AppendText(text + "\n");
        lines.SelectionStart = lines.TextLength; lines.ScrollToCaret();
    }

    void Tick()
    {
        Dictionary<string, object> s = null;
        try { s = Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(FirestarterApp.StatusFile)); } catch { }
        string tid = Get(s, "task_id"), st = Get(s, "state");
        double p = 0;
        if (tid != "" && Get(s, "progress") != "") double.TryParse(Get(s, "progress"), System.Globalization.NumberStyles.Float,
                                                                    System.Globalization.CultureInfo.InvariantCulture, out p);
        frac = tid != "" ? p : 0;
        string el = Get(s, "elapsed");
        task.Text = tid != "" ? "Task " + tid + ": " + Get(s, "task_title") + "   " + (int)(p * 100) + "%" + (el != "" ? ", " + el + " s" : "")
                  : st == "" ? FirestarterApp.Name + " isn't running" : "No task running (" + st + ")";
        task.ForeColor = st == "waiting for free tokens" || st == "paused" || st == "held back" ? Gold : Ink;
        bar.Invalidate();
        try
        {
            if (new FileInfo(StepsFile).Length < pos) { lines.Clear(); lastTask = ""; pos = 0; }   // trimmed
            long end;
            string more = ReadShared(StepsFile, pos, out end);
            pos = end;
            foreach (string l in more.Split(new[] { '\n' }, StringSplitOptions.RemoveEmptyEntries)) AddLine(l);
        }
        catch { }
    }
}

#if !SMITHY
// "WordPress drafts" window: finished WordPress article tasks. Send puts the chosen one on
// the site as a DRAFT (site_wordpress.py via "worker.py wp-send"), after you confirm. Nothing
// is published; each task is sent once.
class WordPressForm : Form
{
    static readonly Color Bg = Color.FromArgb(2, 7, 5), Ink = Color.FromArgb(200, 230, 210),
        Dim = Color.FromArgb(120, 140, 130), Ember = FirestarterApp.Accent, Gold = Color.FromArgb(231, 195, 104),
        PurpleFill = Color.FromArgb(14, 16, 25), PurpleLine = Color.FromArgb(64, 53, 105);

    readonly ListBox list = new ListBox();
    readonly Label detail = new Label(), result = new Label();
    readonly Button send = new Button(), close = new Button();
    readonly List<Dictionary<string, object>> items = new List<Dictionary<string, object>>();

    public WordPressForm()
    {
        Text = "Firestarter: WordPress drafts";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false; MinimizeBox = false;
        StartPosition = FormStartPosition.CenterParent;
        ClientSize = new Size(560, 420);
        AutoAnchor.Fit(this, true);
        BackColor = Bg; ForeColor = Ink;
        Font = new Font("Segoe UI", 10f);

        var head = new Label();
        head.Text = "Finished WordPress articles. Send puts one on your site as a draft: never published.";
        head.ForeColor = Dim; head.SetBounds(16, 12, 528, 40);
        Controls.Add(head);

        list.SetBounds(16, 56, 528, 160);
        list.BackColor = PurpleFill; list.ForeColor = Ink; list.BorderStyle = BorderStyle.FixedSingle;
        list.SelectedIndexChanged += (s, e) => ShowDetail();
        Controls.Add(list);

        detail.SetBounds(16, 224, 528, 96); detail.ForeColor = Ink;
        Controls.Add(detail);
        result.SetBounds(16, 322, 528, 48); result.ForeColor = Gold;
        Controls.Add(result);

        Style(send, "Send as WordPress draft", 16, 376, 250);
        send.Enabled = false;
        send.Click += (s, e) => Send();
        Style(close, "Close", 444, 376, 100);
        close.Click += (s, e) => Close();
        LoadList();
    }

    void Style(Button b, string text, int x, int y, int w)
    {
        b.Text = text; b.SetBounds(x, y, w, 32);
        b.FlatStyle = FlatStyle.Flat;
        b.FlatAppearance.BorderColor = PurpleLine;
        b.BackColor = PurpleFill; b.ForeColor = Ink;
        Controls.Add(b);
    }

    static string Get(Dictionary<string, object> s, string k)
    {
        return s != null && s.ContainsKey(k) && s[k] != null ? Convert.ToString(s[k]) : "";
    }

    void LoadList()
    {
        list.Items.Clear(); items.Clear();
        list.Items.Add("Loading…");
        ThreadPool.QueueUserWorkItem(_ =>
        {
            System.Collections.ArrayList got = null;
            try { got = new JavaScriptSerializer().Deserialize<System.Collections.ArrayList>(FirestarterApp.Run("wp-list")); } catch { }
            try { BeginInvoke((Action)(() => Fill(got))); } catch { }
        });
    }

    void Fill(System.Collections.ArrayList got)
    {
        list.Items.Clear(); items.Clear();
        if (got != null)
            foreach (var o in got)
            {
                var d = o as Dictionary<string, object>;
                if (d == null) continue;
                items.Add(d);
                list.Items.Add("Task " + Get(d, "id") + " (" + Get(d, "kind") + "): " + Get(d, "title") +
                               (Get(d, "sent") != "" ? "   [sent]" : ""));
            }
        if (items.Count == 0)
            list.Items.Add(got == null ? "Couldn't read the task list" : "No finished WordPress articles yet.");
        else list.SelectedIndex = 0;
        ShowDetail();
        if (items.Count == 0)
            detail.Text = got == null ? "Try  worker.py wp-list  in PowerShell to see why."
                                      : "Choose WordPress in the Site row, then ask Firestarter for an article.";
    }

    Dictionary<string, object> Current()
    {
        int i = list.SelectedIndex;
        return i >= 0 && i < items.Count ? items[i] : null;
    }

    void ShowDetail()
    {
        var d = Current();
        if (d == null) { detail.Text = ""; send.Enabled = false; return; }
        bool ready = Get(d, "ready") == "True";
        string sent = Get(d, "sent");
        detail.Text = Get(d, "title") + "\n\n" + Get(d, "excerpt") +
                      (sent != "" ? "\n\nAlready " + sent + "." : !ready ? "\n\nThis result has no title or body to send." : "");
        send.Enabled = ready && sent == "";
    }

    void Send()
    {
        var d = Current();
        if (d == null) return;
        string id = Get(d, "id");
        var ok = ThemedBox.Show(this, "Send Firestarter task " + id + ", \"" + Get(d, "title") + "\", to your WordPress site as a " +
                                 Get(d, "kind") + " DRAFT?\n\nIt won't be published. You can edit or delete it in WordPress.",
                                 "Send as WordPress draft", MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2);
        if (ok != DialogResult.Yes) return;
        send.Enabled = false;
        result.ForeColor = Gold;
        result.Text = "Sending…";
        ThreadPool.QueueUserWorkItem(_ =>
        {
            string o = "";
            try { o = FirestarterApp.Run("wp-send " + id).Trim(); } catch (Exception ex) { o = "Not sent: " + ex.Message; }
            if (o == "") o = "Not sent: something went wrong (try worker.py wp-send " + id + " in PowerShell to see why)";
            try { BeginInvoke((Action)(() => {
                result.ForeColor = o.StartsWith("Sent") ? Ember : Gold;
                result.Text = o;
                LoadList();
            })); } catch { }
        });
    }
}
#endif

#if !SMITHY
// "Site setup" window: a couple of plain questions and the popular extensions for the chosen
// site (from site_profiles.json), each with a one-line note. Save writes them to worker.json
// ("worker.py setup-save"); Firestarter tells its model what the site has. Nothing touches
// the website. An add-on (needs ...) ticks its main extension, and unticking the main one
// unticks the add-on.
class SiteSetupForm : Form
{
    static readonly Color Bg = Color.FromArgb(2, 7, 5), Ink = Color.FromArgb(200, 230, 210),
        Dim = Color.FromArgb(120, 140, 130), Ember = FirestarterApp.Accent, Gold = Color.FromArgb(231, 195, 104),
        PurpleFill = Color.FromArgb(14, 16, 25), PurpleLine = Color.FromArgb(64, 53, 105);

    readonly string site;
    readonly Panel body = new Panel();
    readonly Label result = new Label();
    readonly Button save = new Button(), cancel = new Button();
    readonly Dictionary<string, List<RadioButton>> answers = new Dictionary<string, List<RadioButton>>();
    readonly Dictionary<string, CheckBox> ticks = new Dictionary<string, CheckBox>();
    readonly Dictionary<string, string> needs = new Dictionary<string, string>();
    TextBox other;
    bool linking;

    public SiteSetupForm(string site)
    {
        this.site = site;
        Text = "Firestarter: site setup";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false; MinimizeBox = false;
        StartPosition = FormStartPosition.CenterParent;
        ClientSize = new Size(520, 640);
        AutoAnchor.Fit(this, true);
        BackColor = Bg; ForeColor = Ink;
        Font = new Font("Segoe UI", 10f);

        body.SetBounds(0, 0, 520, 570);
        body.AutoScroll = true;
        Controls.Add(body);
        result.SetBounds(16, 574, 488, 22); result.ForeColor = Gold;
        Controls.Add(result);
        Style(save, "Save", 16, 598, 150);
        save.Enabled = false;
        save.Click += (s, e) => Save();
        Style(cancel, "Cancel", 404, 598, 100);
        cancel.Click += (s, e) => Close();
        // the docs of the extensions ticked (and saved) here, into the shared library (ext_docs.py; Nov 16)
        var docs = new Button();
        Style(docs, "Download docs\u2026", 182, 598, 160);
        docs.Click += (s, e) => { using (var d = new DocsForm(site)) d.ShowDialog(this); };
        AddLabel("Loading\u2026", 16, 16, 480, Dim);
        ThreadPool.QueueUserWorkItem(_ =>
        {
            Dictionary<string, object> m = null;
            try { m = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(FirestarterApp.Run("setup-get " + site)); } catch { }
            try { BeginInvoke((Action)(() => Fill(m))); } catch { }
        });
    }

    void Style(Button b, string text, int x, int y, int w)
    {
        b.Text = text; b.SetBounds(x, y, w, 32);
        b.FlatStyle = FlatStyle.Flat;
        b.FlatAppearance.BorderColor = PurpleLine;
        b.BackColor = PurpleFill; b.ForeColor = Ink;
        Controls.Add(b);
    }

    Label AddLabel(string text, int x, int y, int w, Color c, bool bold = false, float size = 10f)
    {
        var l = new Label();
        l.Text = text; l.ForeColor = c;
        l.Font = new Font("Segoe UI", size, bold ? FontStyle.Bold : FontStyle.Regular);
        l.MaximumSize = new Size(w, 0); l.AutoSize = true;
        l.Location = new Point(x, y);
        body.Controls.Add(l);
        return l;
    }

    static string Get(Dictionary<string, object> s, string k)
    {
        return s != null && s.ContainsKey(k) && s[k] != null ? Convert.ToString(s[k]) : "";
    }

    void Fill(Dictionary<string, object> m)
    {
        body.Controls.Clear();
        if (m == null) { AddLabel("Couldn't read the setup (try  worker.py setup-get  in PowerShell).", 16, 16, 480, Gold); return; }
        int y = 14;
        y = AddLabel(Get(m, "title") + " site setup", 16, y, 480, Ember, true, 13f).Bottom + 4;
        y = AddLabel("Tick what your site has. Firestarter uses this to write the right way. " +
                     "Nothing is sent to your site.", 16, y, 480, Dim).Bottom + 14;

        foreach (var o in (System.Collections.ArrayList)m["questions"])
        {
            var q = (Dictionary<string, object>)o;
            string key = Get(q, "key"), value = Get(q, "value");
            y = AddLabel(Get(q, "ask"), 16, y, 480, Ink, true).Bottom + 2;
            y = AddLabel(Get(q, "why"), 16, y, 480, Dim).Bottom + 4;
            var group = new List<RadioButton>();
            var row = new FlowLayoutPanel();
            row.Location = new Point(16, y); row.Size = new Size(480, 30); row.WrapContents = false;
            foreach (var opt in (System.Collections.ArrayList)q["options"])
            {
                var pair = (System.Collections.ArrayList)opt;
                var r = new RadioButton();
                r.Text = Convert.ToString(pair[1]); r.Tag = Convert.ToString(pair[0]);
                r.ForeColor = Ink; r.AutoSize = true; r.Margin = new Padding(0, 4, 18, 0);
                r.Checked = Convert.ToString(pair[0]) == value;
                group.Add(r); row.Controls.Add(r);
            }
            body.Controls.Add(row);
            answers[key] = group;
            y += 40;
        }

        y = AddLabel("Extensions you have", 16, y, 480, Ink, true).Bottom + 2;
        y = AddLabel("Tick the ones installed on this site.", 16, y, 480, Dim).Bottom + 8;
        foreach (var o in (System.Collections.ArrayList)m["extensions"])
        {
            var x = (Dictionary<string, object>)o;
            string key = Get(x, "key");
            var t = new CheckBox();
            t.Text = Get(x, "name"); t.ForeColor = Ink; t.AutoSize = true;
            t.Checked = Get(x, "on") == "True";
            t.Location = new Point(16, y);
            t.CheckedChanged += (s, e) => Linked(key);
            body.Controls.Add(t);
            ticks[key] = t;
            if (Get(x, "needs") != "") needs[key] = Get(x, "needs");
            y = AddLabel(Get(x, "note"), 36, t.Bottom, 460, Dim).Bottom + 8;
        }

        y = AddLabel("Other extensions (optional)", 16, y + 4, 480, Ink, true).Bottom + 4;
        other = new TextBox();
        other.Text = Get(m, "other"); other.MaxLength = 200;
        other.BackColor = PurpleFill; other.ForeColor = Ink; other.BorderStyle = BorderStyle.FixedSingle;
        other.SetBounds(16, y, 470, 26);
        body.Controls.Add(other);
        y = other.Bottom + 14;

        var adv = new LinkLabel();
        adv.Text = "Advanced: add extensions to this list in site_profiles.json";
        adv.LinkColor = Dim; adv.ActiveLinkColor = Ember; adv.AutoSize = true;
        adv.Location = new Point(16, y);
        adv.LinkClicked += (s, e) => OpenList();
        body.Controls.Add(adv);
        var pad = new Label(); pad.SetBounds(16, adv.Bottom + 8, 10, 8); body.Controls.Add(pad);
        save.Enabled = true;
    }

    // an add-on ticks its main extension; unticking the main one unticks its add-ons
    void Linked(string key)
    {
        if (linking) return;
        linking = true;
        if (ticks[key].Checked && needs.ContainsKey(key) && ticks.ContainsKey(needs[key])) ticks[needs[key]].Checked = true;
        if (!ticks[key].Checked)
            foreach (var n in needs) if (n.Value == key && ticks.ContainsKey(n.Key)) ticks[n.Key].Checked = false;
        linking = false;
    }

    static void OpenList()
    {
        string path = Path.Combine(FirestarterApp.AgentDir, "site_profiles.json");
        string npp = @"C:\Program Files\Notepad++\notepad++.exe";
        try { Process.Start(File.Exists(npp) ? npp : "explorer.exe", "\"" + path + "\""); } catch { }
    }

    void Save()
    {
        var a = new Dictionary<string, object>();
        foreach (var q in answers)
            foreach (var r in q.Value) if (r.Checked) a[q.Key] = (string)r.Tag;
        var on = new List<string>();
        foreach (var t in ticks) if (t.Value.Checked) on.Add(t.Key);
        var data = new Dictionary<string, object> { { "answers", a }, { "installed", on }, { "other", other == null ? "" : other.Text } };
        string file = Path.Combine(Path.GetTempPath(), "firestarter-site-setup.json");
        save.Enabled = false;
        result.Text = "Saving\u2026";
        ThreadPool.QueueUserWorkItem(_ =>
        {
            string o = "";
            try
            {
                File.WriteAllText(file, new JavaScriptSerializer().Serialize(data), new System.Text.UTF8Encoding(false));
                o = FirestarterApp.Run("setup-save " + site + " \"" + file + "\"").Trim();
            }
            catch (Exception ex) { o = ex.Message; }
            finally { try { File.Delete(file); } catch { } }
            try { BeginInvoke((Action)(() => {
                if (o.StartsWith("saved")) { DialogResult = DialogResult.OK; Close(); }
                else { result.Text = "Not saved: " + (o == "" ? "try  worker.py setup-save  in PowerShell" : o); save.Enabled = true; }
            })); } catch { }
        });
    }
}

// "Download docs" (the user, Nov 16): the official docs of the extensions saved in Site setup, into the
// shared library on this PC (library\<site>\extensions\<name>, ext_docs.py), which Othos and Firestarter
// both search. Only the ones she ticks here; public pages only, the site's own docs folder only.
class DocsForm : Form
{
    static readonly Color Bg = Color.FromArgb(2, 7, 5), Ink = Color.FromArgb(200, 230, 210),
        Dim = Color.FromArgb(120, 140, 130), Ember = FirestarterApp.Accent, Gold = Color.FromArgb(231, 195, 104),
        PurpleFill = Color.FromArgb(14, 16, 25), PurpleLine = Color.FromArgb(64, 53, 105), PurpleHover = Color.FromArgb(25, 24, 43);
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

    readonly string site;
    readonly Panel list = new Panel();
    readonly Label status = new Label();
    readonly Button go = new Button(), close = new Button();
    readonly Dictionary<string, CheckBox> ticks = new Dictionary<string, CheckBox>();
    readonly Dictionary<string, string> names = new Dictionary<string, string>();
    Process job;
    bool stopping;

    public DocsForm(string site)
    {
        this.site = site;
        Text = FirestarterApp.Name + ": download docs";
        FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = false; MinimizeBox = false;
        StartPosition = FormStartPosition.CenterParent; ShowInTaskbar = false;
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        BackColor = Bg; ForeColor = Ink; Font = new Font("Segoe UI", 10f);
        ClientSize = new Size(500, 460);

        var stripe = new Panel(); stripe.BackColor = Ember; stripe.SetBounds(0, 0, 6, 460); Controls.Add(stripe);
        var head = new Label(); head.Text = "Download the docs of your extensions"; head.ForeColor = Ember;
        head.Font = new Font("Segoe UI Semibold", 12f); head.SetBounds(22, 14, 460, 26); Controls.Add(head);
        var why = new Label(); why.ForeColor = Dim; why.SetBounds(22, 42, 460, 84);
        why.Text = "The makers' own documentation, kept on this PC so Othos and Firestarter can look things up " +
                   "without the internet. Public pages only, one every 2 seconds. Copyrighted: for your own use, never shared. " +
                   "These are the extensions saved in Site setup.";
        Controls.Add(why);
        list.SetBounds(22, 130, 460, 228); list.AutoScroll = true; list.BackColor = Bg; Controls.Add(list);
        status.SetBounds(22, 364, 460, 46); status.ForeColor = Dim; Controls.Add(status);
        Button(go, "Download", 22, 416, 150, true); go.Enabled = false; go.Click += (s, e) => Start();
        Button(close, "Close", 382, 416, 100, false); close.Click += (s, e) => Close();
        CancelButton = close;                               // Escape closes (and stops a download)
        AutoAnchor.Fit(this, true);
        LoadList();
    }

    void Button(Button b, string text, int x, int y, int w, bool main)
    {
        b.Text = text; b.SetBounds(x, y, w, 32); b.FlatStyle = FlatStyle.Flat;
        b.FlatAppearance.BorderColor = main ? Ember : PurpleLine; b.FlatAppearance.MouseOverBackColor = PurpleHover;
        b.BackColor = PurpleFill; b.ForeColor = Ink; Controls.Add(b);
    }

    static string S(Dictionary<string, object> d, string k)
    { return d != null && d.ContainsKey(k) && d[k] != null ? Convert.ToString(d[k]) : ""; }

    void LoadList()
    {
        status.Text = "Reading the list…";
        ThreadPool.QueueUserWorkItem(_ =>
        {
            System.Collections.ArrayList rows = null;
            try { rows = Json.Deserialize<System.Collections.ArrayList>(FirestarterApp.RunScript("ext_docs.py list " + site)); } catch { }
            try { BeginInvoke((Action)(() => Fill(rows))); } catch { }
        });
    }

    void Fill(System.Collections.ArrayList rows)
    {
        list.Controls.Clear(); ticks.Clear();
        if (rows == null) { status.ForeColor = Gold; status.Text = "Couldn't read the list (try  ext_docs.py list " + site + "  in PowerShell)."; return; }
        if (rows.Count == 0) { status.ForeColor = Dim; status.Text = "No extensions with docs are saved in Site setup yet. Tick them there, Save, then come back."; return; }
        int y = 0;
        foreach (var o in rows)
        {
            var r = (Dictionary<string, object>)o;
            string key = S(r, "key"); int pages = 0; int.TryParse(S(r, "pages"), out pages);
            var t = new CheckBox(); t.AutoSize = true; t.ForeColor = Ink; t.Location = new Point(0, y);
            t.Text = S(r, "name"); t.Checked = pages == 0;            // the ones not downloaded yet
            t.CheckedChanged += (s, e) => go.Enabled = job == null && AnyTicked();
            list.Controls.Add(t); ticks[key] = t; names[key] = S(r, "name");
            var n = new Label(); n.AutoSize = true; n.ForeColor = Dim; n.Location = new Point(20, t.Bottom);
            n.Text = pages == 0 ? "Not downloaded yet" : pages + " pages in the library (tick to fetch again)";
            list.Controls.Add(n);
            y = n.Bottom + 8;
        }
        status.ForeColor = Dim; status.Text = "Tick the ones you want, then Download.";
        go.Enabled = AnyTicked();
    }

    bool AnyTicked() { foreach (var t in ticks.Values) if (t.Checked) return true; return false; }

    void Start()
    {
        var keys = new List<string>();
        foreach (var t in ticks) if (t.Value.Checked) keys.Add(t.Key);
        go.Enabled = false; stopping = false;
        foreach (var t in ticks.Values) t.Enabled = false;
        ThreadPool.QueueUserWorkItem(_ =>
        {
            var said = new List<string>();
            foreach (var key in keys)
            {
                if (stopping) break;
                string name = names[key], final = null; bool ok = false;
                // she ticked it and pressed Download: that's the approval docs_fetch needs
                var p = FirestarterApp.Start("ext_docs.py fetch " + site + " " + key + " --approved");
                job = p;
                string line;
                while ((line = p.StandardOutput.ReadLine()) != null)
                {
                    Dictionary<string, object> ev = null;
                    try { ev = Json.Deserialize<Dictionary<string, object>>(line); } catch { }
                    if (ev == null) continue;
                    string kind = S(ev, "event");
                    if (kind == "progress") { string st = S(ev, "status"), done = S(ev, "done");
                        try { BeginInvoke((Action)(() => { status.ForeColor = Dim; status.Text = name + ": " + done + " pages so far. " + st; })); } catch { } }
                    else if (kind == "done") { ok = true; final = name + ": " + S(ev, "pages") + " pages" +
                        (S(ev, "cut") == "True" ? " (the first ones; the docs are longer)" : "") +
                        (S(ev, "blocked") != "" ? ". " + S(ev, "blocked") : ""); }
                    else if (kind == "error") final = name + ": " + S(ev, "message");
                }
                p.WaitForExit();
                said.Add(final ?? (name + ": stopped."));
                if (!ok && stopping) break;
            }
            job = null;
            try { BeginInvoke((Action)(() => {
                status.ForeColor = Ember; status.Text = string.Join("  ", said.ToArray());
                foreach (var t in ticks.Values) t.Enabled = true;
                if (!stopping) LoadList(); else go.Enabled = AnyTicked();
            })); } catch { }
        });
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        stopping = true;
        var p = job;
        if (p != null && !p.HasExited)
            try { var k = new ProcessStartInfo("taskkill", "/PID " + p.Id + " /T /F"); k.CreateNoWindow = true; k.UseShellExecute = false; Process.Start(k).WaitForExit(10000); } catch { }
        base.OnFormClosing(e);
    }
}
#endif

#if !SMITHY
// "Check my PC" window: reads this PC's specs (Othos's system_specs.py, locally; nothing leaves
// the PC), shows Firestarter's advice for worker.json (worker_specs.py: local CPU models only,
// each with whether it fits), then asks "Apply these settings?". Apply backs up worker.json in
// state\backups first and changes only the advised settings.
class SpecsForm : Form
{
    static readonly Color Bg = Color.FromArgb(2, 7, 5), Ink = Color.FromArgb(200, 230, 210),
        Dim = Color.FromArgb(120, 140, 130), Gold = Color.FromArgb(231, 195, 104),
        Green = Color.FromArgb(120, 200, 140), Red = Color.FromArgb(230, 110, 100),
        PurpleFill = Color.FromArgb(14, 16, 25), PurpleLine = Color.FromArgb(64, 53, 105);

    readonly Label pc = new Label(), question = new Label();
    readonly RichTextBox text = new RichTextBox();
    readonly Button yes = new Button(), no = new Button();
    public bool Applied;

    public SpecsForm(bool check = true)   // (false: the test harness fills it)
    {
        Text = "Firestarter: your PC";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false; MinimizeBox = false;
        StartPosition = FormStartPosition.CenterParent;
        ClientSize = new Size(560, 520);
        AutoAnchor.Fit(this, true);
        BackColor = Bg; ForeColor = Ink;
        Font = new Font("Segoe UI", 10f);
        pc.SetBounds(16, 12, 528, 40); pc.ForeColor = Gold; pc.Text = "Checking your PC\u2026 (this reads its hardware only)";
        Controls.Add(pc);
        text.SetBounds(16, 56, 528, 316);
        text.ReadOnly = true; text.BorderStyle = BorderStyle.None;
        text.BackColor = Bg; text.ForeColor = Ink;
        Controls.Add(text);
        question.SetBounds(16, 376, 528, 96); question.ForeColor = Ink;
        Controls.Add(question);
        Style(yes, "Yes, apply", 300, 476, 120);
        Style(no, "Close", 428, 476, 116);
        yes.Visible = false;
        yes.Click += (s, e) => Apply();
        no.Click += (s, e) => Close();
        if (check) ThreadPool.QueueUserWorkItem(_ =>
        {
            string o;
            try { o = FirestarterApp.Run("specs-check"); } catch (Exception ex) { o = ex.Message; }
            try { BeginInvoke((Action)(() => Fill(o))); } catch { }
        });
    }

    void Style(Button b, string t, int x, int y, int w)
    {
        b.Text = t; b.SetBounds(x, y, w, 32);
        b.FlatStyle = FlatStyle.Flat;
        b.FlatAppearance.BorderColor = PurpleLine;
        b.BackColor = PurpleFill; b.ForeColor = Ink;
        Controls.Add(b);
    }

    static string Get(Dictionary<string, object> d, string k)
    {
        return d != null && d.ContainsKey(k) && d[k] != null ? Convert.ToString(d[k]) : "";
    }

    void Add(string t, Color c, bool bold)
    {
        text.SelectionStart = text.TextLength;
        text.SelectionColor = c;
        text.SelectionFont = new Font("Segoe UI", 10f, bold ? FontStyle.Bold : FontStyle.Regular);
        text.AppendText(t);
    }

    // (public for the test harness)
    public void Fill(string o)
    {
        Dictionary<string, object> m = null;
        try { m = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(o); } catch { }
        if (m == null || m.ContainsKey("error"))
        {
            pc.Text = "Couldn't check this PC.";
            question.Text = m != null ? Get(m, "error") : o.Trim();
            return;
        }
        pc.Text = Get(m, "pc");
        Add("Advice for Firestarter (it runs on the CPU, on this PC):\n\n", Dim, false);
        var advice = m.ContainsKey("advice") ? m["advice"] as System.Collections.ArrayList : null;
        if (advice != null)
            foreach (var a in advice)
            {
                var d = a as Dictionary<string, object>;
                string fit = Get(d, "fit");
                Color c = fit == "fits" ? Green : fit == "tight" ? Gold : Red;
                Add((fit == "fits" ? "\u2713 " : fit == "tight" ? "~ " : "\u2717 ") + Get(d, "area") + ":  ", c, true);
                Add(Get(d, "advice") + "\n", Ink, false);
            }
        Add("\nModels aren't changed here: get them with Get models, then pick them in the model boxes.", Dim, false);
        var changes = m.ContainsKey("changes") ? m["changes"] as System.Collections.ArrayList : null;
        if (changes == null || changes.Count == 0)
            question.Text = "Your settings already match this advice. Nothing to change.";
        else
        {
            var lines = new List<string>();
            foreach (var c in changes) lines.Add(Convert.ToString(c));
            question.Text = "Apply these settings?\n    " + string.Join("\n    ", lines.ToArray()) +
                "\nworker.json is backed up first (state\\backups). Firestarter then restarts its worker; a task that's running starts over.";
            yes.Visible = true;
            no.Text = "No";
        }
    }

    void Apply()
    {
        yes.Enabled = no.Enabled = false;
        ThreadPool.QueueUserWorkItem(_ =>
        {
            string o;
            try { o = FirestarterApp.Run("specs-apply"); } catch (Exception ex) { o = ex.Message; }
            try
            {
                BeginInvoke((Action)(() =>
                {
                    Dictionary<string, object> m = null;
                    try { m = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(o); } catch { }
                    Applied = m != null && m.ContainsKey("applied");
                    question.Text = Applied ? "Applied. worker.json was backed up in state\\backups." : "Not applied: " + o.Trim();
                    yes.Visible = false; no.Text = "Close"; no.Enabled = true;
                }));
            }
            catch { }
        });
    }
}
#endif

#if !SMITHY
// "ZOO items" window (Joomla): 1. add your ZOO app's CSV export once, 2. ask for new items
// (by voice or command line), 3. import the new-....csv file in ZOO yourself, then Mark
// imported keeps the app's list up to date. The steps match plain ZOO or Essentials for
// ZOO (Site setup). The work is done by zoo_csv.py through "worker.py zoo-...".
class ZooForm : Form
{
    static readonly Color Bg = Color.FromArgb(2, 7, 5), Ink = Color.FromArgb(200, 230, 210),
        Dim = Color.FromArgb(120, 140, 130), Ember = FirestarterApp.Accent, Gold = Color.FromArgb(231, 195, 104),
        PurpleFill = Color.FromArgb(14, 16, 25), PurpleLine = Color.FromArgb(64, 53, 105);

    readonly Panel body = new Panel();
    readonly Label result = new Label();
    readonly List<Dictionary<string, object>> ready = new List<Dictionary<string, object>>();
    ListBox files;
    TextBox appName;

    public ZooForm()
    {
        Text = "Firestarter: ZOO items";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false; MinimizeBox = false;
        StartPosition = FormStartPosition.CenterParent;
        ClientSize = new Size(560, 680);
        AutoAnchor.Fit(this, true);
        BackColor = Bg; ForeColor = Ink;
        Font = new Font("Segoe UI", 10f);
        body.SetBounds(0, 0, 560, 600);
        body.AutoScroll = true;
        Controls.Add(body);
        result.SetBounds(16, 604, 528, 36); result.ForeColor = Gold;
        Controls.Add(result);
        var close = new Button();
        Style(close, "Close", 444, 640, 100, false);
        close.Click += (s, e) => Close();
        LoadList();
    }

    void Style(Button b, string text, int x, int y, int w, bool inBody)
    {
        b.Text = text; b.SetBounds(x, y, w, 32);
        b.FlatStyle = FlatStyle.Flat;
        b.FlatAppearance.BorderColor = PurpleLine;
        b.BackColor = PurpleFill; b.ForeColor = Ink;
        if (inBody) body.Controls.Add(b); else Controls.Add(b);
    }

    Label AddLabel(string text, int x, int y, int w, Color c, bool bold = false, float size = 10f)
    {
        var l = new Label();
        l.Text = text; l.ForeColor = c;
        l.Font = new Font("Segoe UI", size, bold ? FontStyle.Bold : FontStyle.Regular);
        l.MaximumSize = new Size(w, 0); l.AutoSize = true;
        l.Location = new Point(x, y);
        body.Controls.Add(l);
        return l;
    }

    static string Get(Dictionary<string, object> s, string k)
    {
        return s != null && s.ContainsKey(k) && s[k] != null ? Convert.ToString(s[k]) : "";
    }

    void LoadList()
    {
        ThreadPool.QueueUserWorkItem(_ =>
        {
            Dictionary<string, object> m = null;
            try { m = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(FirestarterApp.Run("zoo-list")); } catch { }
            try { BeginInvoke((Action)(() => Fill(m))); } catch { }
        });
    }

    int Steps(System.Collections.ArrayList steps, int y)
    {
        int n = 1;
        foreach (var st in steps) y = AddLabel(n++ + ".  " + Convert.ToString(st), 24, y, 500, Ink).Bottom + 3;
        return y;
    }

    void Fill(Dictionary<string, object> m)
    {
        int keep = -body.AutoScrollPosition.Y;
        body.Controls.Clear();
        ready.Clear();
        if (m == null) { AddLabel("Couldn't read the ZOO list (try  worker.py zoo-list  in PowerShell).", 16, 16, 520, Gold); return; }
        var g = (Dictionary<string, object>)m["guide"];
        int y = 14;
        y = AddLabel("ZOO items", 16, y, 520, Ember, true, 13f).Bottom + 4;
        y = AddLabel("New ZOO items as a file you import yourself. Firestarter never logs in to your site.",
                     16, y, 520, Dim).Bottom + 8;
        if (Get(m, "zoo") != "True")
            y = AddLabel("ZOO isn't ticked in Site setup (Joomla). Tick it there so Firestarter knows.",
                         16, y, 520, Gold).Bottom + 8;

        // 1. the export, once per app
        y = AddLabel("1.  Add your ZOO app (once)", 16, y + 4, 520, Ink, true).Bottom + 4;
        y = Steps((System.Collections.ArrayList)g["export"], y);
        var link = new LinkLabel();
        link.Text = Get(g, "link_text"); link.LinkColor = Ember; link.AutoSize = true;
        link.Location = new Point(24, y + 2);
        string url = Get(g, "link");
        link.LinkClicked += (s, e) => { try { Process.Start(url); } catch { } };
        body.Controls.Add(link);
        y = link.Bottom + 8;
        AddLabel("App name", 24, y + 5, 90, Dim);
        appName = new TextBox();
        appName.SetBounds(110, y + 2, 200, 26);
        appName.BackColor = PurpleFill; appName.ForeColor = Ink; appName.BorderStyle = BorderStyle.FixedSingle;
        body.Controls.Add(appName);
        var choose = new Button();
        Style(choose, "Choose CSV…", 320, y, 150, true);
        choose.Click += (s, e) => AddExport();
        y += 40;
        var apps = (System.Collections.ArrayList)m["apps"];
        if (apps.Count == 0) y = AddLabel("No ZOO app added yet.", 24, y, 500, Dim).Bottom + 6;
        foreach (var o in apps)
        {
            var a = (Dictionary<string, object>)o;
            y = AddLabel(Get(a, "name") + ": " + Get(a, "items") + " items, " + Get(a, "columns") + " columns (added " +
                         Get(a, "added") + ")", 24, y, 500, Ink).Bottom + 3;
        }

        // 2. asking for items
        y = AddLabel("2.  Ask for new items", 16, y + 10, 520, Ink, true).Bottom + 4;
        y = AddLabel("Say: “Firestarter, add ZOO items: three yarn bowls, oak, maple and cherry”. " +
                     "It writes them as a file with your app's columns. (Command line: worker.py add zoo BRIEF.)",
                     24, y, 500, Dim).Bottom + 6;

        // 3. importing them
        y = AddLabel("3.  Import them in ZOO", 16, y + 10, 520, Ink, true).Bottom + 4;
        y = Steps((System.Collections.ArrayList)g["import"], y);
        y = AddLabel(Get(g, "note"), 24, y + 2, 500, Dim).Bottom + 8;
        files = new ListBox();
        files.SetBounds(24, y, 500, 96);
        files.BackColor = PurpleFill; files.ForeColor = Ink; files.BorderStyle = BorderStyle.FixedSingle;
        body.Controls.Add(files);
        foreach (var o in (System.Collections.ArrayList)m["ready"])
        {
            var r = (Dictionary<string, object>)o;
            ready.Add(r);
            files.Items.Add("Task " + Get(r, "id") + ": " + Get(r, "items") + " new items for " + Get(r, "app") +
                            (Get(r, "imported") == "True" ? "   [imported]" : ""));
        }
        if (ready.Count == 0) files.Items.Add("No new item files yet.");
        else files.SelectedIndex = 0;
        y = files.Bottom + 6;
        var show = new Button();
        Style(show, "Show file", 24, y, 150, true);
        show.Click += (s, e) => ShowFile();
        var mark = new Button();
        Style(mark, "Mark imported", 184, y, 170, true);
        mark.Click += (s, e) => MarkImported();
        var pad = new Label(); pad.SetBounds(16, y + 40, 10, 10); body.Controls.Add(pad);
        body.AutoScrollPosition = new Point(0, keep);
    }

    Dictionary<string, object> Current()
    {
        int i = files == null ? -1 : files.SelectedIndex;
        return i >= 0 && i < ready.Count ? ready[i] : null;
    }

    void AddExport()
    {
        string name = (appName.Text ?? "").Replace("\"", "").Trim();
        if (name == "") { result.Text = "Type the app's name first (for example Products)."; return; }
        using (var d = new OpenFileDialog())
        {
            d.Title = "Your ZOO CSV export";
            d.Filter = "CSV files (*.csv)|*.csv|All files (*.*)|*.*";
            if (d.ShowDialog(this) != DialogResult.OK) return;
            string file = d.FileName;
            result.ForeColor = Gold; result.Text = "Adding…";
            Run("zoo-add \"" + name + "\" \"" + file + "\"");
        }
    }

    void ShowFile()
    {
        var r = Current();
        if (r == null) return;
        try { Process.Start("explorer.exe", "/select,\"" + Get(r, "file") + "\""); } catch { }
    }

    void MarkImported()
    {
        var r = Current();
        if (r == null) return;
        if (Get(r, "imported") == "True") { result.Text = "That file is already marked imported."; return; }
        var ok = ThemedBox.Show(this, "Did you import task " + Get(r, "id") + "'s file in ZOO?\n\nThis adds its " +
                                 Get(r, "items") + " items to your " + Get(r, "app") + " list. It changes nothing on your site.",
                                 "Mark imported", MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2);
        if (ok != DialogResult.Yes) return;
        Run("zoo-imported " + Get(r, "id"));
    }

    void Run(string args)
    {
        ThreadPool.QueueUserWorkItem(_ =>
        {
            string o = "";
            try { o = FirestarterApp.Run(args).Trim(); } catch (Exception ex) { o = ex.Message; }
            if (o == "") o = "Something went wrong (try  worker.py " + args + "  in PowerShell).";
            try { BeginInvoke((Action)(() => {
                result.ForeColor = o.StartsWith("Not") || o.StartsWith("Something") ? Gold : Ember;
                result.Text = o;
                LoadList();
            })); } catch { }
        });
    }
}
#endif
