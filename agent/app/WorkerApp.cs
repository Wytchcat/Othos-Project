// WorkerApp.cs: the taskbar app of each Othos CPU worker, built twice:
//   Firestarter.exe (site work, worker.py)      build-firestarter.ps1
//   Smithy.exe      (coding, smithy.py)         build-smithy.ps1 (compiled with /define:SMITHY)
// Opening it starts the worker (hidden). Closing its window (X, or right-click the
// taskbar button -> Close window) stops it. It runs on its own, independently of
// the Othos app and of the other worker. The window shows what the worker is doing,
// has the model switcher (applies from the next task), Get models and the work log;
// the Smithy's also has Code chat (Othos's typed code chat, http://127.0.0.1:3000/code);
// Firestarter's also has the Site row (Joomla / WordPress, Site setup), ZOO items and WordPress drafts
// (sends a finished article to WordPress as a draft, only when you press Send).
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
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        FormBorderStyle = FormBorderStyle.FixedSingle; MaximizeBox = false; MinimizeBox = false;
        StartPosition = FormStartPosition.CenterParent;
        ClientSize = new Size(620, 560);
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
#if !SMITHY
    Button siteJoomla, siteWp;
    Label siteSummary;
#endif
    bool checkingPage;
    int pageMisses = 2;
    readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
    Process worker;
    bool loadingModels, stopping;

    public FirestarterForm()
    {
        Text = FirestarterApp.Name;
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
#if SMITHY
        ClientSize = new Size(420, 176 + 36 * FirestarterApp.Tiers.Length + 128);
#else
        ClientSize = new Size(420, 176 + 36 * FirestarterApp.Tiers.Length + 116 + 166);
#endif
        BackColor = Bg; ForeColor = Ink;
        Font = new Font("Segoe UI", 10f);

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
        var logButton = MakeButton("Work log", 150, y2 + 44, 250);
        logButton.Click += (s, e) => OpenWorkLog();
#if SMITHY
        // Othos's code chat: needs Othos's page server (port 3000), so it's off until Othos runs
        codeChat = MakeButton("Code chat", 150, y2 + 82, 250);
        codeChat.Enabled = false;
        codeChat.Click += (s, e) => FirestarterApp.OpenPage("http://127.0.0.1:3000/code");
#else
        var wpButton = MakeButton("WordPress drafts\u2026", 150, y2 + 82, 250);
        wpButton.Click += (s, e) => { using (var d = new WordPressForm()) d.ShowDialog(this); };
        var zooButton = MakeButton("ZOO items\u2026", 150, y2 + 120, 250);
        zooButton.Click += (s, e) => { using (var d = new ZooForm()) d.ShowDialog(this); };
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

    protected override void OnShown(EventArgs e)
    {
        base.OnShown(e);
        StartWorker();
        LoadModels();
#if !SMITHY
        LoadSite();
#endif
        RefreshStatus();
        timer.Start();
        WindowState = FormWindowState.Minimized;   // lives on the taskbar
    }

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
            state.ForeColor = st == "paused" || st == "held back" || st == "waiting for the CPU" ? Gold : Ember;
        }
        string tid = Get(s, "task_id");
        string pct = Get(s, "progress") != "" ? " " + (int)(Convert.ToDouble(Get(s, "progress")) * 100) + "%" : "";
        string el = Get(s, "elapsed") != "" ? ", " + Get(s, "elapsed") + " s" : "";
        var paused = s != null && s.ContainsKey("paused_task") ? s["paused_task"] as Dictionary<string, object> : null;
        task.Text = alive && tid != "" ? "Task " + tid + ": " + (Get(s, "task_title") != "" ? Get(s, "task_title") : Get(s, "task_type")) +
                                         pct + el + " (" + Get(s, "model") + ", " + Get(s, "threads") + " threads)"
                  : alive && paused != null ? "Task " + Get(paused, "id") + " paused: " + Get(paused, "title")
                  : "No task running";
        queue.Text = "Queued: " + (alive ? Get(s, "queued") : "-") + "     Waiting for review: " + (alive ? Get(s, "review") : "-");
        string g = Get(s, "game");
        game.Text = alive && g != "" ? "Game: " + g : "No game running";
        game.ForeColor = alive && g != "" ? Gold : Dim;
        Text = alive ? FirestarterApp.Name + ": " + st : FirestarterApp.Name;
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
        var ok = MessageBox.Show(this, "Send Firestarter task " + id + ", \"" + Get(d, "title") + "\", to your WordPress site as a " +
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
        var ok = MessageBox.Show(this, "Did you import task " + Get(r, "id") + "'s file in ZOO?\n\nThis adds its " +
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
