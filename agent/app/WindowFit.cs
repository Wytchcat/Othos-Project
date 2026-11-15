// WindowFit.cs: the one place that makes a window resizable and keeps it on the screen.
// Rule: no window has a fixed size. Each one gets a sizable border, a minimum size, and an
// opening size that fits the screen's free area (so its buttons can't land under the taskbar).
// The controls inside follow the size with Anchor or Dock (never positions worked out once
// at start-up). Shared by Othos.cs (and any other app in this folder that wants it); compile
// it together with the app: csc ... app\Othos.cs app\WindowFit.cs
using System;
using System.Drawing;
using System.Windows.Forms;

static class WindowFit
{
    // A normal window: sizable, with a maximize button.
    // minClient = the smallest size of the inside (client area) that still works.
    // The opening size is the form's ClientSize as it is when this is called (set it first),
    // made smaller if the screen's free area is.
    public static void Apply(Form f, Size minClient) { Fit(f, minClient, true); }

    // A dialog or pop-up: sizable too, but no maximize or minimize button.
    public static void ApplyDialog(Form f, Size minClient) { Fit(f, minClient, false); }

    static void Fit(Form f, Size minClient, bool maximize)
    {
        Size open = f.ClientSize;
        f.FormBorderStyle = FormBorderStyle.Sizable;
        f.MaximizeBox = maximize;
        if (!maximize) f.MinimizeBox = false;
        // the screen the window will open on (the cursor's), minus the taskbar
        Rectangle wa = Screen.FromPoint(Cursor.Position).WorkingArea;
        f.ClientSize = open;
        Size chrome = new Size(f.Size.Width - f.ClientSize.Width, f.Size.Height - f.ClientSize.Height);
        int maxW = Math.Max(200, wa.Width - chrome.Width), maxH = Math.Max(120, wa.Height - chrome.Height);
        f.MinimumSize = new Size(Math.Min(minClient.Width, maxW) + chrome.Width,
                                 Math.Min(minClient.Height, maxH) + chrome.Height);
        f.ClientSize = new Size(Math.Min(open.Width, maxW), Math.Min(open.Height, maxH));
    }
}
